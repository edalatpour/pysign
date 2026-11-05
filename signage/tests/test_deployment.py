import os
import runpy
from contextlib import closing
from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import OperationalError
from django.http import JsonResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings

from pysign.middleware import ContainerAppsProxyMiddleware
from signage.security import allow_request


class ConfigurationTests(SimpleTestCase):
    def load_settings(self, **environment):
        with patch.dict(os.environ, {"PYSIGN_DEBUG": "1", **environment}, clear=True):
            return runpy.run_path(str(settings.BASE_DIR / "pysign" / "settings.py"))

    def postgres_environment(self, **overrides):
        return {
            "PYSIGN_DATABASE_ENGINE": "postgresql",
            "PYSIGN_POSTGRES_NAME": "pysign",
            "PYSIGN_POSTGRES_HOST": "server.postgres.database.azure.com",
            "PYSIGN_POSTGRES_USER": "app",
            "PYSIGN_POSTGRES_PASSWORD": "password:with/@special?characters",
            **overrides,
        }

    def test_local_defaults_to_sqlite(self):
        config = self.load_settings()
        database = config["DATABASES"]["default"]
        self.assertEqual(database["ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(database["NAME"], str(settings.BASE_DIR / "db.sqlite3"))
        self.assertFalse(config["AZURE_CONTAINER_APPS"])
        self.assertNotIn("SECURE_PROXY_SSL_HEADER", config)

    def test_existing_sqlite_path_override_is_preserved(self):
        config = self.load_settings(PYSIGN_DATABASE="test-local.sqlite3")
        self.assertEqual(config["DATABASES"]["default"]["NAME"], "test-local.sqlite3")

    def test_postgresql_requires_explicit_complete_configuration(self):
        environment = self.postgres_environment()
        for field in ("NAME", "HOST", "USER", "PASSWORD"):
            key = f"PYSIGN_POSTGRES_{field}"
            with self.subTest(field=field), self.assertRaisesMessage(ImproperlyConfigured, key):
                self.load_settings(**{**environment, key: ""})

    def test_postgresql_does_not_parse_or_corrupt_password(self):
        environment = self.postgres_environment()
        database = self.load_settings(**environment)["DATABASES"]["default"]
        self.assertEqual(database["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(database["PASSWORD"], environment["PYSIGN_POSTGRES_PASSWORD"])
        self.assertEqual(database["OPTIONS"], {"sslmode": "verify-full", "connect_timeout": 5})
        self.assertTrue(database["CONN_HEALTH_CHECKS"])
        self.assertEqual(database["CONN_MAX_AGE"], 60)

    def test_postgresql_tls_certificate_and_port_configuration(self):
        config = self.load_settings(**self.postgres_environment(
            PYSIGN_POSTGRES_SSLROOTCERT="root.crt", PYSIGN_POSTGRES_PORT="5433",
        ))
        self.assertEqual(config["DATABASES"]["default"]["OPTIONS"]["sslrootcert"], "root.crt")
        self.assertEqual(config["DATABASES"]["default"]["PORT"], "5433")

    def test_invalid_engine_fails_instead_of_falling_back(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "PYSIGN_DATABASE_ENGINE"):
            self.load_settings(PYSIGN_DATABASE_ENGINE="postgre")

    def test_production_requires_stable_secret(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "PYSIGN_SECRET_KEY"):
            self.load_settings(PYSIGN_DEBUG="0")

    def test_aca_explicitly_trusts_ingress_scheme(self):
        config = self.load_settings(PYSIGN_AZURE_CONTAINER_APPS="1")
        self.assertEqual(config["SECURE_PROXY_SSL_HEADER"], ("HTTP_X_FORWARDED_PROTO", "https"))

    def test_generic_proxy_option_is_preserved(self):
        config = self.load_settings(PYSIGN_TRUST_PROXY="1")
        self.assertEqual(config["SECURE_PROXY_SSL_HEADER"], ("HTTP_X_FORWARDED_PROTO", "https"))
        self.assertFalse(config["AZURE_CONTAINER_APPS"])


@override_settings(AZURE_CONTAINER_APPS=True)
class ProxyTests(SimpleTestCase):
    def request(self, forwarded=None):
        headers = {} if forwarded is None else {"HTTP_X_FORWARDED_FOR": forwarded}
        request = RequestFactory().get("/", REMOTE_ADDR="10.42.0.4", **headers)
        middleware = ContainerAppsProxyMiddleware(
            lambda req: JsonResponse({"ip": req.META["REMOTE_ADDR"]}),
        )
        return middleware(request)

    def test_only_rightmost_aca_address_is_trusted(self):
        response = self.request("spoofed-value, 192.0.2.45")
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"ip": "192.0.2.45"})

    def test_ipv6_is_supported_and_normalized(self):
        response = self.request("192.0.2.1, 2001:0db8::1")
        self.assertJSONEqual(response.content, {"ip": "2001:db8::1"})

    def test_invalid_or_missing_address_is_rejected(self):
        for forwarded in (None, "", "192.0.2.1, invalid", "192.0.2.1:1234"):
            with self.subTest(forwarded=forwarded), self.assertLogs("pysign.middleware", level="WARNING"):
                self.assertEqual(self.request(forwarded).status_code, 400)

    @override_settings(AZURE_CONTAINER_APPS=False)
    def test_local_requests_ignore_forwarded_addresses(self):
        response = self.request("192.0.2.45")
        self.assertJSONEqual(response.content, {"ip": "10.42.0.4"})
        self.assertEqual(self.request().status_code, 200)


@override_settings(AZURE_CONTAINER_APPS=True)
class ProxyRateLimitTests(TestCase):
    def test_spoofing_prefix_does_not_bypass_or_merge_client_limits(self):
        middleware = ContainerAppsProxyMiddleware(
            lambda request: JsonResponse({"allowed": allow_request(request, "test-aca", 1, 300)}),
        )
        factory = RequestFactory()
        for forwarded, allowed in [
            ("192.0.2.1", True),
            ("spoofed, 192.0.2.1", False),
            ("192.0.2.2", True),
        ]:
            with self.subTest(forwarded=forwarded):
                response = middleware(factory.get("/", HTTP_X_FORWARDED_FOR=forwarded))
                self.assertJSONEqual(response.content, {"allowed": allowed})


@override_settings(
    AZURE_CONTAINER_APPS=True,
    SECURE_SSL_REDIRECT=True,
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    ALLOWED_HOSTS=["signage.example.com"],
)
class HealthTests(TestCase):
    def test_direct_liveness_probe_bypasses_ingress_and_never_uses_database(self):
        with self.assertNumQueries(0):
            response = self.client.get("/health/live/", HTTP_HOST="10.42.0.4:8000")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotIn("sessionid", response.cookies)

    def test_direct_readiness_probe_queries_database(self):
        with self.assertNumQueries(1):
            response = self.client.get("/health/ready/", HTTP_HOST="10.42.0.4:8000")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_readiness_failure_is_logged_without_leaking_details(self):
        with patch("pysign.middleware.connection.cursor", side_effect=OperationalError("private details")):
            with self.assertLogs("pysign.middleware", level="WARNING"):
                response = self.client.get("/health/ready/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
        self.assertNotContains(response, "private details", status_code=503)
        self.assertEqual(self.client.get("/health/live/").status_code, 200)

    def test_probes_support_head_but_not_post(self):
        for path in ("/health/live/", "/health/ready/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.head(path).status_code, 200)
                self.assertEqual(self.client.post(path).status_code, 405)

    def test_ordinary_requests_still_require_valid_ingress_headers(self):
        with self.assertLogs("pysign.middleware", level="WARNING"):
            self.assertEqual(self.client.get("/").status_code, 400)

    def test_forwarded_https_does_not_redirect_loop(self):
        response = self.client.get(
            "/", HTTP_HOST="signage.example.com",
            HTTP_X_FORWARDED_PROTO="https", HTTP_X_FORWARDED_FOR="192.0.2.1",
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/accounts/"))


class ProductionStaticTests(SimpleTestCase):
    def test_collected_manifest_css_is_served_without_debug_or_database(self):
        storages = {
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
        }
        with TemporaryDirectory() as root:
            with override_settings(
                DEBUG=False, STATIC_ROOT=root, STORAGES=storages,
                WHITENOISE_USE_FINDERS=False, SECURE_SSL_REDIRECT=False, AZURE_CONTAINER_APPS=False,
            ):
                call_command("collectstatic", interactive=False, stdout=StringIO())
                path = staticfiles_storage.url("signage/style.css")
                self.assertNotEqual(path, "/static/signage/style.css")
                with closing(Client().get(path)) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response["Content-Type"].split(";")[0], "text/css")
                    self.assertEqual(response.charset, "utf-8")
                    self.assertIn("immutable", response["Cache-Control"])

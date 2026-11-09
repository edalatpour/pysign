import os
import runpy
from importlib.util import find_spec
from contextlib import closing
from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import OperationalError
from django.http import JsonResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings

from pisign.middleware import ContainerAppsProxyMiddleware
from pisign.logging import RedactRequestQuery
from signage.security import allow_request


class ConfigurationTests(SimpleTestCase):
    def load_settings(self, **environment):
        with patch.dict(os.environ, {"PISIGN_DEBUG": "1", **environment}, clear=True):
            return runpy.run_path(str(settings.BASE_DIR / "pisign" / "settings.py"))

    def postgres_environment(self, **overrides):
        return {
            "PISIGN_DATABASE_ENGINE": "postgresql",
            "PISIGN_POSTGRES_NAME": "pisign",
            "PISIGN_POSTGRES_HOST": "server.postgres.database.azure.com",
            "PISIGN_POSTGRES_USER": "app",
            "PISIGN_POSTGRES_PASSWORD": "password:with/@special?characters",
            **overrides,
        }

    def test_local_defaults_to_sqlite(self):
        config = self.load_settings()
        database = config["DATABASES"]["default"]
        self.assertEqual(database["ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(database["NAME"], str(settings.BASE_DIR / "db.sqlite3"))
        self.assertFalse(config["AZURE_CONTAINER_APPS"])
        self.assertNotIn("SECURE_PROXY_SSL_HEADER", config)
        self.assertEqual(config["ROOT_URLCONF"], "pisign.urls")
        self.assertEqual(config["WSGI_APPLICATION"], "pisign.wsgi.application")

    def test_existing_sqlite_path_override_is_preserved(self):
        config = self.load_settings(PISIGN_DATABASE="test-local.sqlite3")
        self.assertEqual(config["DATABASES"]["default"]["NAME"], "test-local.sqlite3")

    def test_postgresql_requires_explicit_complete_configuration(self):
        environment = self.postgres_environment()
        for field in ("NAME", "HOST", "USER", "PASSWORD"):
            key = f"PISIGN_POSTGRES_{field}"
            with self.subTest(field=field), self.assertRaisesMessage(ImproperlyConfigured, key):
                self.load_settings(**{**environment, key: ""})

    def test_postgresql_does_not_parse_or_corrupt_password(self):
        environment = self.postgres_environment()
        database = self.load_settings(**environment)["DATABASES"]["default"]
        self.assertEqual(database["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(database["PASSWORD"], environment["PISIGN_POSTGRES_PASSWORD"])
        self.assertEqual(database["OPTIONS"], {"sslmode": "verify-full", "connect_timeout": 5})
        self.assertTrue(database["CONN_HEALTH_CHECKS"])
        self.assertEqual(database["CONN_MAX_AGE"], 60)

    def test_postgresql_tls_certificate_and_port_configuration(self):
        config = self.load_settings(**self.postgres_environment(
            PISIGN_POSTGRES_SSLROOTCERT="root.crt", PISIGN_POSTGRES_PORT="5433",
        ))
        self.assertEqual(config["DATABASES"]["default"]["OPTIONS"]["sslrootcert"], "root.crt")
        self.assertEqual(config["DATABASES"]["default"]["PORT"], "5433")

    def test_invalid_engine_fails_instead_of_falling_back(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "PISIGN_DATABASE_ENGINE"):
            self.load_settings(PISIGN_DATABASE_ENGINE="postgre")

    def sql_environment(self, **overrides):
        return {
            "PISIGN_DATABASE_ENGINE": "mssql",
            "PISIGN_SQL_NAME": "pisign",
            "PISIGN_SQL_HOST": "existing.database.windows.net",
            "PISIGN_SQL_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
            **overrides,
        }

    def test_azure_sql_requires_complete_configuration(self):
        for field in ("NAME", "HOST", "CLIENT_ID"):
            key = f"PISIGN_SQL_{field}"
            with self.subTest(field=field), self.assertRaisesMessage(ImproperlyConfigured, key):
                self.load_settings(**self.sql_environment(**{key: ""}))

    def test_azure_sql_uses_encrypted_managed_identity_connections(self):
        config = self.load_settings(**self.sql_environment())
        database = config["DATABASES"]["default"]
        self.assertEqual(database["ENGINE"], "pisign.azuresql")
        self.assertEqual(database["NAME"], "pisign")
        self.assertEqual(database["HOST"], "existing.database.windows.net")
        self.assertEqual(database["OPTIONS"]["driver"], "ODBC Driver 18 for SQL Server")
        self.assertEqual(database["OPTIONS"]["extra_params"], "Encrypt=yes;TrustServerCertificate=no")
        self.assertEqual(database["OPTIONS"]["managed_identity_client_id"], self.sql_environment()["PISIGN_SQL_CLIENT_ID"])
        self.assertEqual(database["OPTIONS"]["connection_retries"], 0)
        self.assertEqual(database["OPTIONS"]["connection_timeout"], 5)
        self.assertEqual(database["OPTIONS"]["query_timeout"], 5)
        self.assertEqual(database["CONN_MAX_AGE"], 0)
        self.assertFalse(config["DATABASE_CONNECTION_POOLING"])
        self.assertEqual(config["SCHEMA_TO_INSPECT"], "'pisign'")
        self.assertNotIn("PASSWORD", database)
        self.assertNotIn("TOKEN", database)

    def test_azure_sql_migration_query_timeout_override(self):
        config = self.load_settings(**self.sql_environment(PISIGN_SQL_QUERY_TIMEOUT="60"))
        self.assertEqual(config["DATABASES"]["default"]["OPTIONS"]["query_timeout"], 60)
        self.assertEqual(config["DATABASES"]["default"]["OPTIONS"]["connection_timeout"], 5)

    def test_azure_sql_rejects_invalid_query_timeouts(self):
        for value in ("", "0", "-1", "1.5", "invalid"):
            with self.subTest(value=value), self.assertRaisesMessage(ImproperlyConfigured, "PISIGN_SQL_QUERY_TIMEOUT"):
                self.load_settings(**self.sql_environment(PISIGN_SQL_QUERY_TIMEOUT=value))

    def test_production_requires_stable_secret(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "PISIGN_SECRET_KEY"):
            self.load_settings(PISIGN_DEBUG="0")

    def test_aca_explicitly_trusts_ingress_scheme(self):
        config = self.load_settings(
            PISIGN_AZURE_CONTAINER_APPS="1", PISIGN_ASSET_AZURE_ACCOUNT_URL="https://private.blob.core.windows.net",
        )
        self.assertEqual(config["SECURE_PROXY_SSL_HEADER"], ("HTTP_X_FORWARDED_PROTO", "https"))

    def test_aca_requires_durable_private_asset_storage(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "PISIGN_ASSET_AZURE_ACCOUNT_URL"):
            self.load_settings(PISIGN_AZURE_CONTAINER_APPS="1")

    def test_generic_proxy_option_is_preserved(self):
        config = self.load_settings(PISIGN_TRUST_PROXY="1")
        self.assertEqual(config["SECURE_PROXY_SSL_HEADER"], ("HTTP_X_FORWARDED_PROTO", "https"))
        self.assertFalse(config["AZURE_CONTAINER_APPS"])


@skipUnless(find_spec("mssql") and find_spec("azure") and find_spec("azure.identity"), "Install the azure-sql extra for backend tests.")
class AzureSQLBackendTests(SimpleTestCase):
    def test_string_collation_is_scoped_to_new_pisign_columns(self):
        from pisign.azuresql.base import DatabaseWrapper
        from signage.models import ExternalIdentity

        wrapper = DatabaseWrapper({
            "ENGINE": "pisign.azuresql",
            "NAME": "shared",
            "OPTIONS": {"managed_identity_client_id": "test-client"},
        }, alias="azure-sql-test")
        with (
            patch.object(DatabaseWrapper, "sql_server_version", 2022),
            patch.object(wrapper, "cursor", side_effect=AssertionError("Unexpected live database access")),
        ):
            with wrapper.schema_editor(collect_sql=True, atomic=False) as editor:
                editor.create_model(ExternalIdentity)
        sql = " ".join(editor.collected_sql)
        self.assertRegex(sql, r"\[issuer\] nvarchar\(512\)\s+COLLATE Latin1_General_100_BIN2")
        self.assertRegex(sql, r"\[subject\] nvarchar\(255\)\s+COLLATE Latin1_General_100_BIN2")
        self.assertNotIn("ALTER DATABASE", sql)
        self.assertIsNone(ExternalIdentity._meta.get_field("subject").db_collation)

    def test_string_column_changes_keep_case_sensitive_collation(self):
        from pisign.azuresql.base import DatabaseWrapper
        from signage.models import ExternalIdentity

        wrapper = DatabaseWrapper({"NAME": "shared", "OPTIONS": {}}, alias="azure-sql-test")
        editor = wrapper.schema_editor(collect_sql=True, atomic=False)
        field = ExternalIdentity._meta.get_field("subject")
        with patch("mssql.schema.DatabaseSchemaEditor._alter_column_type_sql") as alter:
            editor._alter_column_type_sql(ExternalIdentity, field, field, "nvarchar(256)", None, None)
        self.assertEqual(alter.call_args.args[-1], "Latin1_General_100_BIN2")

    @override_settings(SCHEMA_TO_INSPECT="'pisign'")
    def test_introspection_limits_migration_history_to_pisign_schema(self):
        from pisign.azuresql.base import DatabaseWrapper

        wrapper = DatabaseWrapper({"NAME": "shared", "OPTIONS": {}}, alias="azure-sql-test")
        cursor = MagicMock()
        cursor.fetchall.return_value = [("django_migrations", "BASE TABLE", None)]
        tables = wrapper.introspection.get_table_list(cursor)
        self.assertEqual([table.name for table in tables], ["django_migrations"])
        self.assertIn("TABLE_SCHEMA = 'pisign'", cursor.execute.call_args.args[0])

    def test_connection_requires_isolated_user_and_default_schema(self):
        from pisign.azuresql.base import DatabaseWrapper

        for identity in (("dbo", "pisign-app"), ("pisign", "other-app"), None):
            with self.subTest(identity=identity):
                wrapper = DatabaseWrapper({"NAME": "shared", "OPTIONS": {}}, alias="azure-sql-test")
                connection = MagicMock()
                wrapper.connection = connection
                connection.cursor.return_value.__enter__.return_value.fetchone.return_value = identity
                with patch("mssql.base.DatabaseWrapper.init_connection_state"):
                    with self.assertLogs("pisign.azuresql.base", level="WARNING"):
                        with self.assertRaisesMessage(OperationalError, "default schema pisign"):
                            wrapper.init_connection_state()
                connection.close.assert_called_once()
                self.assertIsNone(wrapper.connection)

        wrapper = DatabaseWrapper({"NAME": "shared", "OPTIONS": {}}, alias="azure-sql-test")
        connection = MagicMock()
        wrapper.connection = connection
        connection.cursor.return_value.__enter__.return_value.fetchone.return_value = ("pisign", "pisign-app")
        with patch("mssql.base.DatabaseWrapper.init_connection_state"):
            wrapper.init_connection_state()
        connection.close.assert_not_called()

    def test_nullable_pairing_code_uses_a_filtered_unique_index(self):
        from pisign.azuresql.base import DatabaseWrapper
        from signage.models import Device

        wrapper = DatabaseWrapper({
            "ENGINE": "pisign.azuresql",
            "NAME": "pisign",
            "OPTIONS": {"managed_identity_client_id": "test-client"},
        }, alias="azure-sql-test")
        with (
            patch.object(DatabaseWrapper, "sql_server_version", 2022),
            patch.object(wrapper, "cursor", side_effect=AssertionError("Unexpected live database access")),
        ):
            with wrapper.schema_editor(collect_sql=True, atomic=False) as editor:
                editor.create_model(Device)
        sql = " ".join(editor.collected_sql)
        self.assertIn("[pairing_code] IS NOT NULL", sql)

    def test_each_new_connection_gets_a_fresh_token(self):
        from pisign.azuresql.base import DatabaseWrapper

        wrapper = DatabaseWrapper({
            "ENGINE": "pisign.azuresql",
            "NAME": "pisign",
            "HOST": "existing.database.windows.net",
            "OPTIONS": {"managed_identity_client_id": "test-client"},
        }, alias="azure-sql-test")
        params = wrapper.get_connection_params()
        with (
            patch("pisign.azuresql.base.ManagedIdentityCredential") as credential,
            patch("mssql.base.DatabaseWrapper.get_new_connection") as connect,
        ):
            get_token = credential.return_value.__enter__.return_value.get_token
            get_token.return_value.token = "first-token"
            wrapper.get_new_connection(params)
            self.assertEqual(connect.call_args.args[0]["TOKEN"], "first-token")
            get_token.return_value.token = "second-token"
            wrapper.get_new_connection(params)
            self.assertEqual(connect.call_args.args[0]["TOKEN"], "second-token")
            self.assertEqual(get_token.call_count, 2)
            get_token.assert_called_with("https://database.windows.net/.default")
            credential.assert_called_with(client_id="test-client")
        self.assertNotIn("TOKEN", params)

    def test_credential_failure_does_not_attempt_a_database_connection(self):
        from azure.core.exceptions import ClientAuthenticationError
        from pisign.azuresql.base import DatabaseWrapper

        wrapper = DatabaseWrapper({
            "ENGINE": "pisign.azuresql",
            "NAME": "pisign",
            "OPTIONS": {"managed_identity_client_id": "test-client"},
        }, alias="azure-sql-test")
        with (
            patch("pisign.azuresql.base.ManagedIdentityCredential") as credential,
            patch("mssql.base.DatabaseWrapper.get_new_connection") as connect,
        ):
            credential.return_value.__enter__.return_value.get_token.side_effect = ClientAuthenticationError("private details")
            with self.assertLogs("pisign.azuresql.base", level="WARNING") as logs:
                with self.assertRaisesMessage(OperationalError, "Azure SQL managed identity authentication failed"):
                    wrapper.get_new_connection(wrapper.get_connection_params())
            self.assertNotIn("private details", " ".join(logs.output))
            connect.assert_not_called()


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
            with self.subTest(forwarded=forwarded), self.assertLogs("pisign.middleware", level="WARNING"):
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
        with patch("pisign.middleware.connection.cursor", side_effect=OperationalError("private details")):
            with self.assertLogs("pisign.middleware", level="WARNING"):
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
        with self.assertLogs("pisign.middleware", level="WARNING"):
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


class RequestLoggingTests(SimpleTestCase):
    def test_configured_server_logger_redacts_authorization_response(self):
        logger = logging.getLogger("django.server")
        with self.assertLogs("django.server", level="INFO") as logs:
            logger.info('"%s" %s %s', (
                "GET /accounts/callback/?code=private-code&state=private-state"
                "&client_info=private-identity HTTP/1.1"
            ), "400", "1216")
        self.assertIn('"GET /accounts/callback/ HTTP/1.1" 400 1216', logs.output[0])
        self.assertNotIn("private-", logs.output[0])
        self.assertNotIn("?", logs.output[0])

    def test_filter_handles_preformatted_requests_and_preserves_ordinary_logs(self):
        filter_ = RedactRequestQuery()
        for message, expected in [
            ('"GET /accounts/callback/?code=private-code HTTP/1.1" 400 1216',
             '"GET /accounts/callback/ HTTP/1.1" 400 1216'),
            ('"GET /accounts/login/ HTTP/1.1" 200 1100',
             '"GET /accounts/login/ HTTP/1.1" 200 1100'),
        ]:
            with self.subTest(message=message):
                record = logging.LogRecord("django.server", logging.INFO, "", 0, message, (), None)
                self.assertTrue(filter_.filter(record))
                self.assertEqual(record.getMessage(), expected)
import logging

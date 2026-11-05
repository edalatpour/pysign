import json
import time
from contextlib import contextmanager
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from joserfc import jwt
from joserfc.jwk import RSAKey
from requests import Response
from requests.exceptions import RequestException

from signage.auth import user_from_claims
from signage.models import ExternalIdentity

TENANT = "12345678-1234-1234-1234-123456789abc"
ISSUER = f"https://example.ciamlogin.com/{TENANT}/v2.0"
AUTH_SETTINGS = {
    "SECURE_SSL_REDIRECT": False,
    "ENTRA_TENANT_ID": TENANT,
    "ENTRA_SUBDOMAIN": "example",
    "ENTRA_CLIENT_ID": "application-id",
    "ENTRA_CLIENT_SECRET": "application-secret",
    "ENTRA_REDIRECT_URI": "https://signage.example.com/accounts/callback/",
}


@override_settings(**AUTH_SETTINGS)
class EntraTests(TestCase):
    def claims(self, **overrides):
        return {"iss": ISSUER, "sub": "consumer-1", "tid": TENANT, **overrides}

    def test_stable_subject_identity_with_no_password(self):
        first = user_from_claims(self.claims(email="first@example.com"))
        second = user_from_claims(self.claims(email="changed@example.com"))
        self.assertEqual(first.pk, second.pk)
        self.assertFalse(first.has_usable_password())
        self.assertEqual(ExternalIdentity.objects.count(), 1)

    def test_email_never_links_different_consumers(self):
        first = user_from_claims(self.claims(email="shared@example.com"))
        second = user_from_claims(self.claims(sub="consumer-2", email="shared@example.com"))
        self.assertNotEqual(first.pk, second.pk)

    def test_different_issuer_cannot_share_identity(self):
        first = user_from_claims(self.claims())
        second = user_from_claims(self.claims(iss="https://different.example"))
        self.assertNotEqual(first.pk, second.pk)

    def test_wrong_tenant_or_missing_subject_rejected(self):
        for claims in [self.claims(tid="different"), self.claims(sub=""), self.claims(tid=None)]:
            with self.assertRaises(ValueError):
                user_from_claims(claims)
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_disabled_user_cannot_sign_in(self):
        user = user_from_claims(self.claims())
        user.is_active = False
        user.save()
        with self.assertRaises(ValueError):
            user_from_claims(self.claims())

    def test_signin_has_no_local_password_form(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, "Continue with Microsoft")
        self.assertContains(response, "Sign in - PiSign")
        self.assertContains(response, "PiSign / Your screens, in sync.")
        self.assertNotContains(response, 'type="password"')
        self.assertEqual(self.client.get("/accounts/signup/").status_code, 404)

    def test_begin_uses_fixed_callback_and_rejects_open_redirect(self):
        remote = Mock()
        remote.initiate_auth_code_flow.return_value = {
            "auth_uri": "https://example.ciamlogin.com/authorize",
        }
        with patch("signage.auth.entra_client", return_value=remote):
            response = self.client.post(reverse("entra-login"), {"next": "https://evil.example/"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["auth_next"], "/")
        self.assertEqual(
            remote.initiate_auth_code_flow.call_args.kwargs["redirect_uri"],
            AUTH_SETTINGS["ENTRA_REDIRECT_URI"],
        )

    def test_begin_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post(reverse("entra-login")).status_code, 403)

    def test_callback_creates_session_only_after_validation(self):
        with self.mock_oidc():
            state = self.start_login(next="/displays/")
            response = self.finish_login(state)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/displays/")
        self.assertIn("_auth_user_id", self.client.session)
        self.assertNotIn("auth_flow", self.client.session)
        self.assertNotIn("auth_next", self.client.session)
        self.assertEqual(set(self.client.session.keys()), {
            "_auth_user_id", "_auth_user_backend", "_auth_user_hash",
        })
        body = parse_qs(self.token_requests[0].body)
        self.assertEqual(body["code_verifier"], [self.code_verifier])
        self.assertEqual(body["redirect_uri"], [AUTH_SETTINGS["ENTRA_REDIRECT_URI"]])

    def test_real_client_rejects_unsolicited_callback(self):
        self.assertEqual(self.client.get(reverse("entra-callback"), {
            "state": "attacker", "code": "attacker",
        }).status_code, 400)
        self.assertEqual(ExternalIdentity.objects.count(), 0)

    @override_settings(ENTRA_CLIENT_SECRET="")
    def test_missing_configuration_fails_explicitly(self):
        self.assertEqual(self.client.post(reverse("entra-login")).status_code, 503)

    @contextmanager
    def mock_oidc(self):
        self.signing_key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
        self.token_requests = []
        now = int(time.time())
        self.token_claims = self.claims(
            aud=AUTH_SETTINGS["ENTRA_CLIENT_ID"], iat=now, exp=now + 600,
        )
        self.token_error = None
        self.jwks_status = 200
        metadata = {
            "issuer": ISSUER,
            "authorization_endpoint": ISSUER + "/authorize",
            "token_endpoint": ISSUER + "/token",
            "jwks_uri": ISSUER + "/keys",
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        keyset = {"keys": [self.signing_key.as_dict(private=False)]}

        def send(session, request, **kwargs):
            status = 200
            if request.url.endswith("/.well-known/openid-configuration"):
                data = metadata
            elif request.url == metadata["jwks_uri"]:
                data = keyset
                status = self.jwks_status
            elif request.url == metadata["token_endpoint"]:
                self.token_requests.append(request)
                data = self.token_error or {
                    "id_token": jwt.encode(
                        {"alg": "RS256", "kid": "test-key"}, self.token_claims, self.signing_key,
                    ),
                    "access_token": "not-retained",
                    "refresh_token": "not-retained",
                    "token_type": "Bearer", "expires_in": 600,
                }
            else:
                raise AssertionError(f"Unexpected OIDC request: {request.method} {request.url}")
            response = Response()
            response.status_code = status
            response._content = json.dumps(data).encode()
            response.headers["Content-Type"] = "application/json"
            response.url = request.url
            return response

        with patch("requests.sessions.Session.send", autospec=True, side_effect=send):
            yield

    def start_login(self, **data):
        response = self.client.post(reverse("entra-login"), data)
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlsplit(response.url).query)
        self.token_claims["nonce"] = query["nonce"][0]
        self.code_verifier = self.client.session["auth_flow"]["code_verifier"]
        return query["state"][0]

    def finish_login(self, state, **data):
        return self.client.get(reverse("entra-callback"), {
            "state": state, "code": "authorization-code", **data,
        })

    def test_real_authorization_uses_state_nonce_and_pkce(self):
        with self.mock_oidc():
            response = self.client.post(reverse("entra-login"))
        query = parse_qs(urlsplit(response.url).query)
        self.assertTrue(query["state"][0])
        self.assertTrue(query["nonce"][0])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["code_challenge"][0])
        self.assertEqual(set(query["scope"][0].split()), {"openid", "profile", "email"})

    def test_real_signed_id_token_validation(self):
        cases = [
            {"aud": "wrong-app"},
            {"aud": [AUTH_SETTINGS["ENTRA_CLIENT_ID"], "other-app"]},
            {"azp": "other-app"},
            {"iss": "https://attacker.example"},
            {"nonce": "wrong"},
            {"nonce": None},
            {"exp": int(time.time()) - 1000},
            {"exp": None},
            {"iat": int(time.time()) + 1000},
            {"tid": "different"},
            {"sub": ""},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.mock_oidc():
                state = self.start_login()
                self.token_claims.update(overrides)
                self.assertEqual(self.finish_login(state).status_code, 400)
                self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(ExternalIdentity.objects.count(), 0)

    def test_multiple_audiences_with_correct_authorized_party(self):
        with self.mock_oidc():
            state = self.start_login()
            self.token_claims.update(
                aud=[AUTH_SETTINGS["ENTRA_CLIENT_ID"], "other-app"],
                azp=AUTH_SETTINGS["ENTRA_CLIENT_ID"],
            )
            self.assertEqual(self.finish_login(state).status_code, 302)

    def test_successful_callback_cannot_be_replayed(self):
        with self.mock_oidc():
            state = self.start_login()
            self.assertEqual(self.finish_login(state).status_code, 302)
            self.assertEqual(self.finish_login(state).status_code, 400)
        self.assertEqual(len(self.token_requests), 1)

    def test_wrong_signature_rejected(self):
        with self.mock_oidc():
            state = self.start_login()
            self.signing_key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
            self.assertEqual(self.finish_login(state).status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_state_mismatch_consumes_flow_without_exchanging_code(self):
        with self.mock_oidc():
            state = self.start_login()
            self.assertEqual(self.finish_login("wrong-state").status_code, 400)
            self.assertEqual(self.finish_login(state).status_code, 400)
        self.assertEqual(self.token_requests, [])
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_expired_flow_rejected_without_exchanging_code(self):
        with self.mock_oidc():
            state = self.start_login()
            session = self.client.session
            session["auth_flow"]["started_at"] -= 601
            session.save()
            self.assertEqual(self.finish_login(state).status_code, 400)
        self.assertEqual(self.token_requests, [])

    def test_provider_errors_and_missing_id_token_rejected(self):
        for result in [{"error": "invalid_grant"}, {"access_token": "not-an-id-token"}]:
            with self.subTest(result=result), self.mock_oidc():
                state = self.start_login()
                self.token_error = result
                self.assertEqual(self.finish_login(state).status_code, 400)
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_user_cancellation_rejected(self):
        with self.mock_oidc():
            state = self.start_login()
            self.assertEqual(self.finish_login(state, code="", error="access_denied").status_code, 400)
        self.assertEqual(self.token_requests, [])

    def test_signing_key_fetch_failure_rejected(self):
        with self.mock_oidc():
            state = self.start_login()
            self.jwks_status = 503
            self.assertEqual(self.finish_login(state).status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_discovery_network_error_reported(self):
        with patch("requests.sessions.Session.send", side_effect=RequestException()):
            self.assertEqual(self.client.post(reverse("entra-login")).status_code, 502)

    def test_missing_session_diagnostic_does_not_log_callback_parameters(self):
        with self.assertLogs("signage.auth", level="WARNING") as logs:
            response = self.client.get(reverse("entra-callback"), {
                "code": "private-code", "state": "private-state",
            })
        self.assertEqual(response.status_code, 400)
        text = "\n".join(logs.output)
        self.assertIn("session flow: Missing OAuth session", text)
        self.assertNotIn("private-code", text)
        self.assertNotIn("private-state", text)

    def test_provider_diagnostic_logs_only_error_code_and_numeric_identifiers(self):
        with self.mock_oidc():
            state = self.start_login()
            self.token_error = {
                "error": "invalid_client",
                "error_codes": [7000215],
                "error_description": "private-provider-response",
            }
            with self.assertLogs("signage.auth", level="WARNING") as logs:
                self.assertEqual(self.finish_login(state).status_code, 400)
        text = "\n".join(logs.output)
        self.assertIn("token exchange: Entra returned invalid_client; AADSTS codes: 7000215", text)
        self.assertNotIn("private-provider-response", text)
        self.assertNotIn(AUTH_SETTINGS["ENTRA_CLIENT_SECRET"], text)

    def test_untrusted_provider_error_fields_are_not_logged(self):
        with self.mock_oidc():
            state = self.start_login()
            self.token_error = {
                "error": "private-value\ninjected-log",
                "error_codes": ["private-value"],
                "error_description": "private-value",
            }
            with self.assertLogs("signage.auth", level="WARNING") as logs:
                self.assertEqual(self.finish_login(state).status_code, 400)
        self.assertIn("unknown_error", "\n".join(logs.output))
        self.assertNotIn("private-value", "\n".join(logs.output))

    def test_invalid_claim_diagnostic_identifies_claim_without_its_value(self):
        with self.mock_oidc():
            state = self.start_login()
            self.token_claims["aud"] = "private-wrong-audience"
            with self.assertLogs("signage.auth", level="WARNING") as logs:
                self.assertEqual(self.finish_login(state).status_code, 400)
        text = "\n".join(logs.output)
        self.assertIn("ID token validation: InvalidClaimError (aud)", text)
        self.assertNotIn("private-wrong-audience", text)

    def test_library_exception_diagnostic_excludes_sensitive_message(self):
        for error in (ValueError("private-state"), RuntimeError("private-nonce")):
            with self.subTest(error=type(error).__name__), self.mock_oidc():
                state = self.start_login()
                remote = Mock()
                remote.acquire_token_by_auth_code_flow.side_effect = error
                with patch("signage.auth.entra_client", return_value=remote):
                    with self.assertLogs("signage.auth", level="WARNING") as logs:
                        self.assertEqual(self.finish_login(state).status_code, 400)
                text = "\n".join(logs.output)
                self.assertIn(f"token exchange: {type(error).__name__}", text)
                self.assertNotIn("private-", text)

    def test_tenant_diagnostic_excludes_identity_values(self):
        with self.mock_oidc():
            state = self.start_login()
            self.token_claims["tid"] = "private-wrong-tenant"
            with self.assertLogs("signage.auth", level="WARNING") as logs:
                self.assertEqual(self.finish_login(state).status_code, 400)
        text = "\n".join(logs.output)
        self.assertIn("account identity: Missing tenant claim or tenant does not match", text)
        self.assertNotIn("private-wrong-tenant", text)

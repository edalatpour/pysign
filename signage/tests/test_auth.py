import time
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from authlib.integrations.base_client.errors import MismatchingStateError
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey

from signage.auth import entra_client, user_from_claims
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
        self.assertNotContains(response, 'type="password"')
        self.assertEqual(self.client.get("/accounts/signup/").status_code, 404)

    def test_begin_uses_fixed_callback_and_rejects_open_redirect(self):
        remote = Mock()
        from django.http import HttpResponseRedirect
        remote.authorize_redirect.return_value = HttpResponseRedirect("https://example.ciamlogin.com/authorize")
        with patch("signage.auth.entra_client", return_value=remote):
            response = self.client.post(reverse("entra-login"), {"next": "https://evil.example/"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["auth_next"], "/")
        self.assertEqual(remote.authorize_redirect.call_args.args[1], AUTH_SETTINGS["ENTRA_REDIRECT_URI"])

    def test_begin_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post(reverse("entra-login")).status_code, 403)

    def test_callback_creates_session_only_after_validation(self):
        remote = Mock()
        remote.authorize_access_token.return_value = {"userinfo": self.claims()}
        with patch("signage.auth.entra_client", return_value=remote):
            self.assertEqual(self.client.get(reverse("entra-callback")).status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_invalid_state_and_signature_return_no_session(self):
        for error in [MismatchingStateError(), JoseError()]:
            remote = Mock()
            remote.authorize_access_token.side_effect = error
            with patch("signage.auth.entra_client", return_value=remote):
                self.assertEqual(self.client.get(reverse("entra-callback")).status_code, 400)
            self.assertNotIn("_auth_user_id", self.client.session)

    def test_real_client_rejects_unsolicited_callback(self):
        self.assertEqual(self.client.get(reverse("entra-callback"), {
            "state": "attacker", "code": "attacker",
        }).status_code, 400)
        self.assertEqual(ExternalIdentity.objects.count(), 0)

    @override_settings(ENTRA_CLIENT_SECRET="")
    def test_missing_configuration_fails_explicitly(self):
        self.assertEqual(self.client.post(reverse("entra-login")).status_code, 503)

    def oidc_remote(self):
        remote = entra_client()
        remote.server_metadata.update({
            "issuer": ISSUER,
            "authorization_endpoint": ISSUER + "/authorize",
            "token_endpoint": ISSUER + "/token",
            "id_token_signing_alg_values_supported": ["RS256"],
        })
        return remote

    def test_real_authorization_uses_state_nonce_and_pkce(self):
        remote = self.oidc_remote()
        with patch.object(remote, "load_server_metadata", return_value=remote.server_metadata):
            with patch("signage.auth.entra_client", return_value=remote):
                response = self.client.post(reverse("entra-login"))
        query = parse_qs(urlsplit(response.url).query)
        self.assertTrue(query["state"][0])
        self.assertTrue(query["nonce"][0])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["code_challenge"][0])

    def test_real_signed_id_token_validation(self):
        remote = self.oidc_remote()
        key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
        keyset = {"keys": [key.as_dict(private=False)]}
        now = int(time.time())
        valid = self.claims(aud=AUTH_SETTINGS["ENTRA_CLIENT_ID"], iat=now, exp=now + 600, nonce="expected")
        cases = [
            ({}, key, True),
            ({"aud": "wrong-app"}, key, False),
            ({"iss": "https://attacker.example"}, key, False),
            ({"nonce": "wrong"}, key, False),
            ({"exp": now - 1000}, key, False),
            ({}, RSAKey.generate_key(2048, parameters={"kid": "test-key"}), False),
        ]
        with patch.object(remote, "load_server_metadata", return_value=remote.server_metadata):
            with patch.object(remote, "fetch_jwk_set", return_value=keyset):
                for overrides, signing_key, should_pass in cases:
                    token = jwt.encode({"alg": "RS256", "kid": "test-key"}, {**valid, **overrides}, signing_key)
                    with self.subTest(overrides=overrides, should_pass=should_pass):
                        if should_pass:
                            self.assertEqual(remote.parse_id_token(
                                {"id_token": token}, nonce="expected",
                            )["sub"], valid["sub"])
                        else:
                            with self.assertRaises(JoseError):
                                remote.parse_id_token({"id_token": token}, nonce="expected")

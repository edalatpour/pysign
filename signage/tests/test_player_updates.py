import base64
import json
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from player.releases import canonical, digest_file
from signage.models import Device, PlayerRelease, PlayerRestart
from signage.security import digest_token


@override_settings(SECURE_SSL_REDIRECT=False)
class PlayerUpdateTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.public = self.root / "public.pem"
        self.private = self.root / "private.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "Ed25519", "-out", str(self.private)],
                       check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", str(self.private), "-pubout", "-out", str(self.public)],
                       check=True, capture_output=True)
        settings = override_settings(
            PLAYER_RELEASE_PUBLIC_KEY=str(self.public), ASSET_STORAGE_ROOT=self.root / "storage",
            ASSET_AZURE_ACCOUNT_URL="",
        )
        settings.enable()
        self.addCleanup(settings.disable)
        self.owner = get_user_model().objects.create_user("owner")
        self.other = get_user_model().objects.create_user("other")
        self.token = "a" * 43
        self.device = Device.objects.create(owner=self.owner, token_digest=digest_token(self.token))
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}
        self.archive = self.root / "release.tar.gz"
        self.archive.write_bytes(b"signed archive bytes")
        self.envelope_path = self.root / "release.json"

    def publish(self, sequence=1, version="1.0.0"):
        manifest = {
            "sequence": sequence, "version": version, "size": self.archive.stat().st_size,
            "sha256": digest_file(self.archive), "machines": ["aarch64"], "python": ["3.11"],
            "updater_protocol": 1, "state_schema": 1,
        }
        source = self.root / "manifest"
        signature = self.root / "signature"
        source.write_bytes(canonical(manifest))
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(self.private), "-rawin",
             "-in", str(source), "-out", str(signature)], check=True, capture_output=True,
        )
        envelope = {"manifest": manifest, "signature": base64.b64encode(signature.read_bytes()).decode()}
        self.envelope_path.write_text(json.dumps(envelope))
        call_command("publish_player_release", self.envelope_path, self.archive, verbosity=0)
        return envelope

    def report(self, **changes):
        return {
            "installed_version": "bootstrap", "installed_sequence": 0, "target_version": "1.0.0",
            "status": "rolled_back", "error": "Startup failed", "updater_protocol": 1, **changes,
        }

    def test_owner_publication_targets_every_enrolled_device_and_reconnect(self):
        envelope = self.publish()
        Device.objects.create(owner=self.other, token_digest=digest_token("b" * 43))
        Device.objects.create(token_digest=digest_token("c" * 43))
        for token in ("a" * 43, "b" * 43, "c" * 43):
            response = self.client.get(reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {token}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["release"], envelope)
        newer = self.publish(2, "1.0.1")
        self.assertEqual(self.client.get(reverse("api-config"), **self.headers).json()["release"], newer)
        self.assertEqual(self.client.get(reverse("api-config"), **self.headers).json()["release"], newer)

    def test_release_download_uses_existing_device_auth_and_private_storage(self):
        self.publish()
        url = reverse("api-release", args=[1])
        self.assertEqual(self.client.get(url).status_code, 401)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 401)
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), self.archive.read_bytes())
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(self.client.head(url, **self.headers)["Content-Length"], str(self.archive.stat().st_size))
        self.device.delete()
        self.assertEqual(self.client.get(url, **self.headers).status_code, 401)

    def test_reports_are_scoped_validated_and_displayed_with_errors(self):
        report = self.report(error="<script>startup</script>")
        response = self.client.post(reverse("api-config"), {"update": report},
                                    content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, 200)
        self.device.refresh_from_db()
        self.assertEqual(self.device.player_update, report)
        self.assertIsNotNone(self.device.update_reported_at)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("device", args=[self.device.pk]))
        self.assertContains(response, "&lt;script&gt;startup&lt;/script&gt;")
        self.assertContains(response, "rolled_back")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("device", args=[self.device.pk])).status_code, 404)
        for body in ([], {}, {"update": None}, {"update": self.report(installed_sequence=True)},
                     {"update": self.report(status="arbitrary")}, {"update": self.report(error="x" * 1001)}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(reverse("api-config"), body,
                                                 content_type="application/json", **self.headers).status_code, 400)

    def test_legacy_poll_and_remote_restart_remain_compatible(self):
        self.publish()
        restart = PlayerRestart.objects.create(
            device=self.device, requested_by=self.owner, requested_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        response = self.client.get(reverse("api-config"), **self.headers)
        self.assertEqual(response.json()["restart"]["id"], str(restart.request_id))
        self.device.refresh_from_db()
        self.assertEqual(self.device.player_update, {})
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("dashboard")), "bootstrap required")

    def test_publication_rejects_bad_signatures_hashes_and_replayed_sequences(self):
        self.publish()
        with self.assertRaises(CommandError):
            call_command("publish_player_release", self.envelope_path, self.archive)
        self.archive.write_bytes(b"tampered")
        with self.assertRaises(CommandError):
            call_command("publish_player_release", self.envelope_path, self.archive)
        envelope = json.loads(self.envelope_path.read_text())
        envelope["manifest"]["sequence"] = 2
        self.envelope_path.write_text(json.dumps(envelope))
        with self.assertRaises(CommandError):
            call_command("publish_player_release", self.envelope_path, self.archive)
        self.assertEqual(PlayerRelease.objects.count(), 1)

    def test_account_admin_has_no_publication_endpoint(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("api-config"), {"update": self.report()},
                                    content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(PlayerRelease.objects.exists())

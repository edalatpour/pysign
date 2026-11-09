import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from signage.models import Device, PlayerReset, PlayerRestart, RateLimit, Slide
from signage.security import digest_token


@override_settings(SECURE_SSL_REDIRECT=False)
class PlatformTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner")
        self.other = get_user_model().objects.create_user("other")
        self.token = "a" * 43
        self.device = Device.objects.create(
            name="Scoreboard", owner=self.owner, token_digest=digest_token(self.token),
        )
        self.other_device = Device.objects.create(
            name="Other private display", owner=self.other, token_digest=digest_token("b" * 43),
        )
        self.slide = Slide.objects.create(
            device=self.device, title="Scores", url="https://example.com/scores", duration=5,
        )
        self.client.force_login(self.owner)

    def device_url(self, name="device", device=None, **kwargs):
        return reverse(name, kwargs={"device_id": (device or self.device).pk, **kwargs})

    def test_dashboard_only_lists_owned_displays(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Scoreboard")
        self.assertNotContains(response, self.other_device.name)

    def test_new_url_defaults_to_one_hour(self):
        response = self.client.get(self.device_url())
        self.assertEqual(response.context["slide_form"]["duration"].value(), 3600)
        self.assertEqual(Slide().duration, 3600)

    def test_qr_checkbox_defaults_to_checked_on_add_and_edit(self):
        self.assertTrue(Slide().show_qr)
        for url, form_name in [
            (self.device_url(), "slide_form"),
            (self.device_url("add-slide"), "form"),
            (self.device_url("edit-slide", slide_id=self.slide.pk), "form"),
        ]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertTrue(response.context[form_name]["show_qr"].value())
                self.assertContains(response, "Show QR code")
                self.assertIn('checked', str(response.context[form_name]["show_qr"]))

    def test_qr_checkbox_persists_and_is_sent_per_url(self):
        data = {
            "title": "No QR", "url": "https://example.com/private",
            "duration": 30, "position": 1,
        }
        self.assertEqual(self.client.post(self.device_url("add-slide"), data).status_code, 302)
        added = self.device.slides.get(title="No QR")
        self.assertFalse(added.show_qr)
        url = self.device_url("edit-slide", slide_id=self.slide.pk)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.slide.refresh_from_db()
        self.assertFalse(self.slide.show_qr)
        response = self.client.get(url)
        self.assertFalse(response.context["form"]["show_qr"].value())
        self.assertNotIn("checked", str(response.context["form"]["show_qr"]))
        self.assertEqual(self.client.post(url, {**data, "show_qr": "on"}).status_code, 302)
        self.slide.refresh_from_db()
        self.assertTrue(self.slide.show_qr)
        response = self.client.get(reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual([slide["show_qr"] for slide in response.json()["slides"]], [True, False])

    def test_custom_qr_url_is_optional_saved_and_can_be_cleared(self):
        response = self.client.get(self.device_url())
        self.assertEqual(response.context["slide_form"]["qr_url"].value(), "")
        self.assertContains(response, "QR code URL")
        self.assertContains(response, "Leave blank to use the current page URL")
        target = "https://example.org/scan?source=display#info"
        data = {
            "title": "Custom QR", "url": "https://example.com/content",
            "duration": 30, "position": 1, "show_qr": "on", "qr_url": target,
        }
        self.assertEqual(self.client.post(self.device_url("add-slide"), data).status_code, 302)
        slide = self.device.slides.get(title="Custom QR")
        self.assertEqual(slide.url, data["url"])
        self.assertEqual(slide.qr_url, target)
        url = self.device_url("edit-slide", slide_id=slide.pk)
        self.assertEqual(self.client.get(url).context["form"]["qr_url"].value(), target)
        response = self.client.get(reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response.json()["slides"][1]["qr_url"], target)
        self.assertEqual(response.json()["slides"][0]["qr_url"], "")
        disabled = {key: value for key, value in data.items() if key != "show_qr"}
        self.assertEqual(self.client.post(url, disabled).status_code, 302)
        slide.refresh_from_db()
        self.assertFalse(slide.show_qr)
        self.assertEqual(slide.qr_url, target)
        self.assertEqual(self.client.post(url, {**data, "qr_url": ""}).status_code, 302)
        slide.refresh_from_db()
        self.assertEqual(slide.qr_url, "")

    def test_custom_qr_url_validation(self):
        for target in [
            "javascript:alert(1)", "file:///tmp/content", "not-a-url",
            "https://user:password@example.com/", "https://example.com/" + "a" * 2048,
        ]:
            with self.subTest(target=target):
                response = self.client.post(self.device_url("add-slide"), {
                    "title": "Bad QR", "url": "https://example.com/content",
                    "duration": 30, "position": 1, "qr_url": target,
                })
                self.assertEqual(response.status_code, 200)
                self.assertIn("qr_url", response.context["form"].errors)
        self.assertEqual(self.device.slides.count(), 1)

    def test_dashboard_includes_brand_and_app_icons(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'aria-label="PiSign home"')
        for asset in ("logo-light.svg", "app-icon.svg", "app-icon-180.png", "favicon.ico"):
            self.assertContains(response, f"/static/pisign/brand/{asset}")

    def test_unauthenticated_management_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.device_url()).status_code, 302)

    def test_every_management_surface_rejects_other_owner(self):
        foreign_slide = Slide.objects.create(
            device=self.other_device, title="Private", url="https://example.org",
        )
        paths = [
            ("device", {}),
            ("remove-device", {}),
            ("restart-player", {}),
            ("reset-player", {}),
            ("add-slide", {}),
            ("edit-slide", {"slide_id": foreign_slide.pk}),
            ("remove-slide", {"slide_id": foreign_slide.pk}),
        ]
        for name, kwargs in paths:
            with self.subTest(name=name):
                response = self.client.post(
                    self.device_url(name, self.other_device, **kwargs),
                    {"name": "Stolen", "title": "Bad", "url": "https://evil.example", "duration": 1, "position": 0},
                )
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get(self.device_url(device=self.other_device)).status_code, 404)
        self.other_device.refresh_from_db()
        self.assertEqual(self.other_device.name, "Other private display")
        self.assertEqual(foreign_slide.device_id, self.other_device.pk)

    def test_slide_id_cannot_be_mixed_with_owned_device(self):
        foreign_slide = Slide.objects.create(
            device=self.other_device, title="Private", url="https://example.org",
        )
        for name in ("edit-slide", "remove-slide"):
            self.assertEqual(self.client.post(
                self.device_url(name, slide_id=foreign_slide.pk),
                {"title": "Stolen", "url": "https://example.com", "duration": 5, "position": 0},
            ).status_code, 404)

    def test_playlist_changes_and_order(self):
        for title, duration, position in [("Calendar", 5, 2), ("Scores first", 10, 1)]:
            response = self.client.post(self.device_url("add-slide"), {
                "title": title, "url": "https://example.com/" + str(position),
                "duration": duration, "position": position,
            })
            self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {self.token}")
        slides = response.json()["slides"]
        self.assertEqual([slide["title"] for slide in slides], ["Scores", "Scores first", "Calendar"])
        self.assertEqual([slide["duration"] for slide in slides], [5, 10, 5])
        self.assertEqual(set(slides[0]), {"title", "url", "duration", "show_qr", "qr_url"})
        self.assertEqual(response["Cache-Control"], "no-store")
        self.device.refresh_from_db()
        self.assertTrue(self.device.online)

    def test_api_credential_cannot_read_arbitrary_device(self):
        response = self.client.get(
            reverse("api-config") + f"?device_id={self.other_device.pk}",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(response.json()["name"], self.device.name)
        self.assertNotContains(response, self.other_device.name)

    def test_session_is_not_a_device_credential(self):
        self.assertEqual(self.client.get(reverse("api-config")).status_code, 401)
        self.assertEqual(self.client.get(
            reverse("api-config"), HTTP_AUTHORIZATION="Bearer incorrect",
        ).status_code, 401)

    def test_device_token_is_not_a_user_session(self):
        client = Client()
        response = client.post(
            self.device_url("remove-device"), HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Device.objects.filter(pk=self.device.pk).exists())

    def test_url_and_duration_validation(self):
        invalid = [
            ("javascript:alert(1)", 5), ("file:///etc/passwd", 5),
            ("https://user:password@example.com", 5),
            ("https://example.com", 0), ("https://example.com", 86401),
        ]
        for url, duration in invalid:
            with self.subTest(url=url, duration=duration):
                response = self.client.post(self.device_url("add-slide"), {
                    "title": "Bad", "url": url, "duration": duration, "position": 0,
                })
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
        self.assertEqual(self.device.slides.count(), 1)

    def test_mutations_require_csrf_and_post(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        for name, kwargs in [
            ("remove-device", {}), ("restart-player", {}),
            ("reset-player", {}),
            ("remove-slide", {"slide_id": self.slide.pk}),
        ]:
            url = self.device_url(name, **kwargs)
            self.assertEqual(csrf_client.post(url).status_code, 403)
            self.assertEqual(csrf_client.get(url).status_code, 405)
        self.assertEqual(csrf_client.post(reverse("claim"), {"code": "ABCDE23456", "name": "Bad"}).status_code, 403)

    def test_disconnect_revokes_device_credential(self):
        self.client.post(self.device_url("remove-device"))
        self.assertEqual(self.client.get(
            reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {self.token}",
        ).status_code, 401)
        self.assertFalse(Slide.objects.filter(pk=self.slide.pk).exists())

    def test_revocation_is_under_collapsed_advanced_settings(self):
        response = self.client.get(self.device_url())
        self.assertContains(response, "<summary>Advanced settings</summary>", html=True)
        self.assertContains(response, "<h2>Revoke device access</h2>", html=True)
        self.assertContains(response, "lost, stolen, or compromised device")
        self.assertContains(response, "It does not remotely erase browser cookies.")
        self.assertContains(response, "Use this to transfer, retire, or re-pair a working display.")
        self.assertNotContains(response, "Disconnect permanently")
        content = response.content.decode()
        advanced = content[content.index('<details class="card">'):content.index("</details>")]
        self.assertNotIn(" open", advanced.split(">", 1)[0])
        self.assertIn(reverse("remove-device", kwargs={"device_id": self.device.pk}), advanced)
        self.assertLess(content.index("<h2>Reset / unpair</h2>"), content.index("<details"))

    def test_restart_requires_user_session_not_device_token(self):
        client = Client()
        self.assertEqual(client.post(
            self.device_url("restart-player"), HTTP_AUTHORIZATION=f"Bearer {self.token}",
        ).status_code, 302)
        self.assertFalse(PlayerRestart.objects.exists())

    def queue_restart(self):
        self.assertEqual(self.client.post(self.device_url("restart-player")).status_code, 302)
        return PlayerRestart.objects.get(device=self.device)

    def complete_restart(self, request_id, token=None):
        return self.client.post(
            reverse("api-restart-completed", kwargs={"request_id": request_id}),
            HTTP_AUTHORIZATION=f"Bearer {token or self.token}",
        )

    def test_restart_is_expiring_deduplicated_and_visible(self):
        now = timezone.now()
        with patch("signage.views.timezone.now", return_value=now):
            restart = self.queue_restart()
            self.queue_restart()
        self.assertEqual(PlayerRestart.objects.count(), 1)
        self.assertEqual(restart.requested_by, self.owner)
        self.assertEqual(restart.requested_at, now)
        self.assertEqual(restart.expires_at, now + timedelta(minutes=10))
        self.assertEqual(restart.status, "pending")
        restart.refresh_from_db()
        original_id = restart.request_id
        response = self.client.get(reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response.json()["restart"], {
            "id": str(original_id), "expires_at": restart.expires_at.isoformat(),
        })
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertContains(self.client.get(self.device_url()), "Restart pending")
        self.queue_restart()
        restart.refresh_from_db()
        self.assertEqual(restart.request_id, original_id)
        self.assertEqual(restart.expires_at, now + timedelta(minutes=10))

    def test_restart_acknowledgement_is_idempotent_and_preserves_device(self):
        restart = self.queue_restart()
        response = self.complete_restart(restart.request_id)
        self.assertEqual(response.json(), {"status": "completed"})
        self.assertEqual(response["Cache-Control"], "no-store")
        restart.refresh_from_db()
        completed_at = restart.completed_at
        self.assertIsNotNone(completed_at)
        self.assertEqual(self.complete_restart(restart.request_id).json(), {"status": "completed"})
        restart.refresh_from_db()
        self.assertEqual(restart.completed_at, completed_at)
        self.assertEqual(restart.status, "completed")
        config = self.client.get(reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {self.token}").json()
        self.assertNotIn("restart", config)
        self.device.refresh_from_db()
        self.assertEqual(self.device.owner, self.owner)
        self.assertEqual(self.device.token_digest, digest_token(self.token))
        self.assertEqual(self.device.slides.count(), 1)
        self.assertContains(self.client.get(self.device_url()), "Player restarted and reconnected")
        new_restart = self.queue_restart()
        self.assertNotEqual(new_restart.request_id, restart.request_id)
        self.assertIsNone(new_restart.completed_at)

    def test_expired_restart_is_not_delivered_and_can_be_replaced(self):
        restart = self.queue_restart()
        PlayerRestart.objects.filter(device=self.device).update(expires_at=timezone.now() - timedelta(seconds=1))
        restart.refresh_from_db()
        self.assertEqual(restart.status, "expired")
        self.assertNotIn("restart", self.client.get(
            reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {self.token}",
        ).json())
        self.assertEqual(self.complete_restart(restart.request_id).json(), {"status": "expired"})
        self.assertContains(self.client.get(self.device_url()), "expired without confirmation")
        replacement = self.queue_restart()
        self.assertNotEqual(replacement.request_id, restart.request_id)
        self.assertEqual(self.complete_restart(restart.request_id).json(), {"status": "obsolete"})
        replacement.refresh_from_db()
        self.assertIsNone(replacement.completed_at)

    def test_completion_requires_correct_device_credential_and_post(self):
        restart = self.queue_restart()
        url = reverse("api-restart-completed", kwargs={"request_id": restart.request_id})
        self.assertEqual(self.client.post(url).status_code, 401)
        self.assertEqual(self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {self.token}").status_code, 405)
        self.assertEqual(self.complete_restart(restart.request_id, token="b" * 43).json(), {"status": "obsolete"})
        self.assertEqual(self.complete_restart(uuid.uuid4()).json(), {"status": "obsolete"})
        restart.refresh_from_db()
        self.assertIsNone(restart.completed_at)
        self.assertEqual(self.complete_restart(restart.request_id, token="c" * 43).status_code, 401)

    def test_disconnect_deletes_restart_and_prevents_completion(self):
        restart = self.queue_restart()
        self.client.post(self.device_url("remove-device"))
        self.assertFalse(PlayerRestart.objects.exists())
        self.assertEqual(self.complete_restart(restart.request_id).status_code, 401)

    def test_unpaired_device_cannot_receive_or_complete_restart(self):
        restart = self.queue_restart()
        self.device.owner = None
        self.device.save()
        self.assertEqual(self.client.get(
            reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {self.token}",
        ).json(), {"state": "unpaired", "slides": []})
        self.assertEqual(self.complete_restart(restart.request_id).json(), {"status": "obsolete"})
        restart.refresh_from_db()
        self.assertIsNone(restart.completed_at)

    def register_device(self):
        response = self.client.post(
            reverse("api-register"), json.dumps({"name": "Pi Zero"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        credentials = response.json()
        device = Device.objects.get(pk=credentials["device_id"])
        return device, credentials["token"]

    def queue_reset(self):
        self.assertEqual(self.client.post(
            self.device_url("reset-player"), {"confirm": "on"},
        ).status_code, 302)
        return PlayerReset.objects.get(device=self.device)

    def complete_reset(self, request_id, token=None):
        return self.client.post(
            reverse("api-reset-completed", kwargs={"request_id": request_id}),
            HTTP_AUTHORIZATION=f"Bearer {token or self.token}",
        )

    def test_reset_requires_confirmation_and_user_session(self):
        response = self.client.post(self.device_url("reset-player"), follow=True)
        self.assertContains(response, "Confirm deletion")
        self.assertFalse(PlayerReset.objects.exists())
        client = Client()
        self.assertEqual(client.post(
            self.device_url("reset-player"), {"confirm": "on"},
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        ).status_code, 302)
        self.assertFalse(PlayerReset.objects.exists())

    def test_reset_is_durable_deduplicated_and_prioritized_over_restart(self):
        self.queue_restart()
        now = timezone.now()
        with patch("signage.views.timezone.now", return_value=now):
            reset = self.queue_reset()
            repeated = self.queue_reset()
        self.assertEqual(reset.request_id, repeated.request_id)
        self.assertEqual(reset.expires_at, now + timedelta(minutes=10))
        self.assertEqual(reset.requested_by, self.owner)
        self.assertEqual(PlayerReset.objects.count(), 1)
        response = self.client.get(reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.json()["reset"], {
            "id": str(reset.request_id), "expires_at": reset.expires_at.isoformat(),
        })
        self.assertNotIn("restart", response.json())
        self.assertContains(self.client.get(self.device_url()), "Reset pending")
        self.device.refresh_from_db()
        self.assertEqual(self.device.owner, self.owner)
        self.assertEqual(self.device.slides.count(), 1)

    def test_reset_unpairs_and_clears_playlist_without_revoking_credential(self):
        self.queue_restart()
        reset = self.queue_reset()
        response = self.complete_reset(reset.request_id)
        self.assertEqual(response.json(), {"status": "completed"})
        self.assertEqual(response["Cache-Control"], "no-store")
        self.device.refresh_from_db()
        self.assertIsNone(self.device.owner_id)
        self.assertIsNone(self.device.pairing_code)
        self.assertIsNone(self.device.pairing_expires_at)
        self.assertEqual(self.device.token_digest, digest_token(self.token))
        self.assertFalse(self.device.slides.exists())
        self.assertFalse(PlayerRestart.objects.exists())
        self.assertNotContains(self.client.get(reverse("dashboard")), "Scoreboard")
        self.assertEqual(self.client.get(self.device_url()).status_code, 404)
        self.assertEqual(self.client.get(
            reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {self.token}",
        ).json(), {"state": "unpaired", "slides": []})
        code = self.client.post(
            reverse("api-pairing"), HTTP_AUTHORIZATION=f"Bearer {self.token}",
        ).json()["code"]
        self.client.force_login(self.other)
        self.client.post(reverse("claim"), {"code": code, "name": "New owner"})
        completed_at = PlayerReset.objects.get(device=self.device).completed_at
        self.assertEqual(self.complete_reset(reset.request_id).json(), {"status": "completed"})
        self.device.refresh_from_db()
        self.assertEqual(self.device.owner, self.other)
        self.assertEqual(PlayerReset.objects.get(device=self.device).completed_at, completed_at)

    def test_reset_expiry_limits_delivery_but_not_delayed_completion(self):
        reset = self.queue_reset()
        PlayerReset.objects.filter(device=self.device).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertNotIn("reset", self.client.get(
            reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {self.token}",
        ).json())
        self.assertContains(self.client.get(self.device_url()), "Reset request expired")
        self.assertEqual(self.complete_reset(reset.request_id).json(), {"status": "completed"})

    def test_reset_replacement_and_completion_are_device_scoped(self):
        reset = self.queue_reset()
        url = reverse("api-reset-completed", kwargs={"request_id": reset.request_id})
        self.assertEqual(self.client.post(url).status_code, 401)
        self.assertEqual(self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {self.token}").status_code, 405)
        self.assertEqual(self.complete_reset(reset.request_id, "b" * 43).json(), {"status": "obsolete"})
        self.assertEqual(self.complete_reset(uuid.uuid4()).json(), {"status": "obsolete"})
        PlayerReset.objects.filter(device=self.device).update(expires_at=timezone.now() - timedelta(seconds=1))
        replacement = self.queue_reset()
        self.assertNotEqual(replacement.request_id, reset.request_id)
        self.assertEqual(self.complete_reset(reset.request_id).json(), {"status": "obsolete"})
        self.device.refresh_from_db()
        self.assertEqual(self.device.owner, self.owner)
        self.client.post(self.device_url("remove-device"))
        self.assertFalse(PlayerReset.objects.exists())
        self.assertEqual(self.complete_reset(replacement.request_id).status_code, 401)

    def test_registration_stores_only_token_digest(self):
        device, token = self.register_device()
        self.assertEqual(device.token_digest, digest_token(token))
        self.assertNotEqual(device.token_digest, token)
        self.assertIsNone(device.owner_id)
        self.assertEqual(len(device.pairing_code), 10)
        self.assertGreater(device.pairing_expires_at, timezone.now())

    def test_registration_cannot_set_owner_or_playlist(self):
        response = self.client.post(reverse("api-register"), json.dumps({
            "owner": self.owner.pk, "slides": [{"url": "https://example.com"}],
        }), content_type="application/json")
        device = Device.objects.get(pk=response.json()["device_id"])
        self.assertIsNone(device.owner_id)
        self.assertEqual(device.slides.count(), 0)

    def test_pairing_code_is_single_use_and_not_in_config(self):
        device, token = self.register_device()
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        config = self.client.get(reverse("api-config"), **headers).json()
        self.assertEqual(config, {"state": "unpaired", "slides": []})
        pairing = self.client.post(reverse("api-pairing"), **headers).json()
        code = pairing["code"]
        response = self.client.post(reverse("claim"), {
            "code": code[:5] + "-" + code[5:], "name": "Front desk",
        })
        self.assertEqual(response.status_code, 302)
        device.refresh_from_db()
        self.assertEqual(device.owner, self.owner)
        self.assertIsNone(device.pairing_code)
        self.client.force_login(self.other)
        response = self.client.post(reverse("claim"), {"code": code, "name": "Stolen"})
        self.assertContains(response, "invalid, expired, or already used")
        self.assertEqual(self.client.post(reverse("api-pairing"), **headers).status_code, 409)
        device.refresh_from_db()
        self.assertEqual(device.owner, self.owner)

    def test_expired_code_is_rejected_and_refreshes(self):
        device, token = self.register_device()
        old_code = device.pairing_code
        device.pairing_expires_at = timezone.now() - timedelta(seconds=1)
        device.save()
        response = self.client.post(reverse("claim"), {"code": old_code, "name": "No"})
        self.assertContains(response, "invalid, expired, or already used")
        response = self.client.post(
            reverse("api-pairing"), HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["code"], old_code)

    def test_claim_attempts_are_rate_limited(self):
        with patch("signage.security.time.time", return_value=1000):
            for _ in range(10):
                self.assertEqual(self.client.post(
                    reverse("claim"), {"code": "NOTVALID12", "name": "No"},
                ).status_code, 200)
            self.assertEqual(self.client.post(
                reverse("claim"), {"code": "NOTVALID12", "name": "No"},
            ).status_code, 429)

    def test_rate_limit_is_database_backed(self):
        self.client.post(reverse("claim"), {"code": "NOTVALID12", "name": "No"})
        self.assertEqual(RateLimit.objects.count(), 2)

    def test_invalid_registration_input(self):
        for body in ("[1]", "{", '{"name": 2}', '{"name": ""}'):
            self.assertEqual(self.client.post(
                reverse("api-register"), body, content_type="application/json",
            ).status_code, 400)

    def test_user_content_is_escaped(self):
        self.device.name = "<script>alert(1)</script>"
        self.device.save()
        response = self.client.get(self.device_url())
        self.assertNotContains(response, "<script>")
        self.assertContains(response, "&lt;script&gt;")

    def test_device_and_slide_edits_and_removal(self):
        self.assertEqual(self.client.post(self.device_url(), {"name": "Renamed"}).status_code, 302)
        self.assertEqual(self.client.post(self.device_url("edit-slide", slide_id=self.slide.pk), {
            "title": "Updated", "url": "https://example.org", "duration": 8, "position": 4,
        }).status_code, 302)
        self.slide.refresh_from_db()
        self.assertEqual(self.slide.duration, 8)
        self.client.post(self.device_url("remove-slide", slide_id=self.slide.pk))
        self.assertEqual(self.device.slides.count(), 0)

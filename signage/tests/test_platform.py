import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from signage.models import Device, RateLimit, Slide
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
        self.assertEqual(set(slides[0]), {"title", "url", "duration"})
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
        for name, kwargs in [("remove-device", {}), ("remove-slide", {"slide_id": self.slide.pk})]:
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

    def register_device(self):
        response = self.client.post(
            reverse("api-register"), json.dumps({"name": "Pi Zero"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        credentials = response.json()
        device = Device.objects.get(pk=credentials["device_id"])
        return device, credentials["token"]

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

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from player.client import validate_config
from signage.models import Device, Playlist, Slide
from signage.security import digest_token


@override_settings(SECURE_SSL_REDIRECT=False)
class PlaylistTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner")
        self.other = get_user_model().objects.create_user("other")
        self.playlist = Playlist.objects.create(owner=self.owner, name="Lobby")
        self.other_playlist = Playlist.objects.create(owner=self.other, name="Private")
        self.token = "a" * 43
        self.device = Device.objects.create(
            owner=self.owner, name="Lobby screen", playlist=self.playlist,
            token_digest=digest_token(self.token),
        )
        self.second_token = "b" * 43
        self.second_device = Device.objects.create(
            owner=self.owner, name="Second screen", playlist=self.playlist,
            token_digest=digest_token(self.second_token),
        )
        self.foreign_device = Device.objects.create(
            owner=self.other, name="Private screen", playlist=self.other_playlist,
            token_digest=digest_token("c" * 43),
        )
        self.slide = Slide.objects.create(
            playlist=self.playlist, title="Welcome", url="https://example.com/welcome",
        )
        self.client.force_login(self.owner)

    def playlist_url(self, name="playlist", playlist=None, **kwargs):
        return reverse(name, kwargs={"playlist_id": (playlist or self.playlist).pk, **kwargs})

    def config(self, token=None):
        return self.client.get(
            reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {token or self.token}",
        ).json()

    def test_top_level_playlists_are_private_and_include_counts(self):
        response = self.client.get(reverse("playlists"))
        self.assertContains(response, self.playlist.name)
        self.assertNotContains(response, self.other_playlist.name)
        self.assertContains(response, "1 entry / 2 displays")
        self.assertContains(response, reverse("create-playlist"))
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, reverse("playlists"))
        self.assertContains(response, f"Playlist: {self.playlist.name}")

    def test_create_and_rename_playlist(self):
        response = self.client.post(reverse("create-playlist"), {"name": "Announcements"})
        playlist = self.owner.playlists.get(name="Announcements")
        self.assertRedirects(response, self.playlist_url(playlist=playlist))
        self.assertFalse(playlist.devices.exists())
        response = self.client.post(self.playlist_url(playlist=playlist), {"name": "News"})
        self.assertRedirects(response, self.playlist_url(playlist=playlist))
        playlist.refresh_from_db()
        self.assertEqual(playlist.name, "News")
        self.assertEqual(playlist.owner, self.owner)

    def test_name_validation_and_duplicate_names_are_scoped_to_owner(self):
        for name in ("", " ", "a" * 101, self.playlist.name):
            with self.subTest(name=name):
                response = self.client.post(reverse("create-playlist"), {"name": name})
                self.assertEqual(response.status_code, 200)
                self.assertIn("name", response.context["form"].errors)
        response = self.client.post(reverse("create-playlist"), {"name": self.other_playlist.name})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.owner.playlists.count(), 2)
        response = self.client.post(self.playlist_url(), {"name": self.other_playlist.name})
        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.playlist.refresh_from_db()
        self.assertEqual(self.playlist.name, "Lobby")
        self.assertEqual(self.client.post(self.playlist_url(), {"name": "Lobby"}).status_code, 302)

    def test_display_details_selects_owned_playlists_and_links_to_creation(self):
        response = self.client.get(reverse("device", args=[self.device.pk]))
        self.assertQuerySetEqual(
            response.context["form"].fields["playlist"].queryset, [self.playlist],
        )
        self.assertEqual(response.context["form"]["playlist"].value(), self.playlist.pk)
        self.assertContains(response, reverse("create-device-playlist", args=[self.device.pk]))
        self.assertContains(response, self.playlist_url())
        self.assertNotContains(response, "Add a URL")
        self.assertNotContains(response, self.other_playlist.name)

    def test_creating_from_display_assigns_only_after_valid_submission(self):
        url = reverse("create-device-playlist", args=[self.device.pk])
        response = self.client.get(url)
        self.assertContains(response, "Create playlist")
        self.assertContains(response, self.device.name)
        self.device.refresh_from_db()
        self.assertEqual(self.device.playlist, self.playlist)
        self.assertEqual(self.client.post(url, {"name": ""}).status_code, 200)
        self.device.refresh_from_db()
        self.assertEqual(self.device.playlist, self.playlist)
        response = self.client.post(url, {"name": "New loop", "owner": self.other.pk})
        created = self.owner.playlists.get(name="New loop")
        self.assertRedirects(response, self.playlist_url(playlist=created))
        self.device.refresh_from_db()
        self.second_device.refresh_from_db()
        self.assertEqual(self.device.playlist, created)
        self.assertEqual(self.second_device.playlist, self.playlist)
        self.assertEqual(self.config()["slides"], [])

    def test_foreign_display_cannot_be_used_to_create_playlist(self):
        url = reverse("create-device-playlist", args=[self.foreign_device.pk])
        for method in (self.client.get, self.client.post):
            self.assertEqual(method(url, {"name": "Stolen"}).status_code, 404)
        self.assertFalse(self.owner.playlists.filter(name="Stolen").exists())

    def test_assign_switch_and_clear_playlist_without_changing_other_displays(self):
        alternate = Playlist.objects.create(owner=self.owner, name="Alternative")
        slide = Slide.objects.create(
            playlist=alternate, title="News", url="https://example.com/news", duration=10,
        )
        url = reverse("device", args=[self.device.pk])
        self.assertEqual(self.client.post(url, {
            "name": self.device.name, "playlist": alternate.pk,
        }).status_code, 302)
        self.assertEqual([item["title"] for item in self.config()["slides"]], [slide.title])
        self.assertEqual([item["title"] for item in self.config(self.second_token)["slides"]], [self.slide.title])
        self.assertEqual(self.client.post(url, {
            "name": self.device.name, "playlist": "",
        }).status_code, 302)
        self.device.refresh_from_db()
        self.assertIsNone(self.device.playlist_id)
        self.assertEqual(self.config(), {"state": "paired", "name": self.device.name, "slides": []})
        self.assertTrue(self.playlist.slides.exists())
        self.assertContains(self.client.get(url), "No playlist assigned")

    def test_foreign_and_nonexistent_playlist_assignments_are_rejected(self):
        for playlist_id in (self.other_playlist.pk, 99999, "invalid"):
            with self.subTest(playlist=playlist_id):
                response = self.client.post(reverse("device", args=[self.device.pk]), {
                    "name": "Changed", "playlist": playlist_id,
                })
                self.assertEqual(response.status_code, 200)
                self.assertIn("playlist", response.context["form"].errors)
                self.device.refresh_from_db()
                self.assertEqual(self.device.name, "Lobby screen")
                self.assertEqual(self.device.playlist, self.playlist)

    def test_shared_edits_keep_player_api_shape_and_list_order(self):
        response = self.client.post(self.playlist_url("add-slide"), {
            "title": "First", "url": "https://example.com/first", "duration": 12,
            "position": 0, "show_qr": "on", "qr_url": "https://example.com/scan",
        })
        self.assertEqual(response.status_code, 302)
        expected = self.config()["slides"]
        self.assertEqual(set(self.config()), {"state", "name", "slides"})
        self.assertEqual([slide["title"] for slide in expected], ["Welcome", "First"])
        self.assertEqual(set(expected[0]), {"title", "url", "duration", "show_qr", "qr_url"})
        self.assertEqual(self.config(self.second_token)["slides"], expected)
        for token in (self.token, self.second_token):
            config = self.config(token)
            self.assertEqual(validate_config(config), config)
        self.assertEqual(self.client.post(self.playlist_url("edit-slide", slide_id=self.slide.pk), {
            "title": "Last", "url": "https://example.com/last", "duration": 25,
            "position": 2,
        }).status_code, 302)
        for token in (self.token, self.second_token):
            self.assertEqual([slide["title"] for slide in self.config(token)["slides"]], ["First", "Last"])
        self.client.post(self.playlist_url("remove-slide", slide_id=self.slide.pk))
        self.assertEqual(self.config()["slides"], self.config(self.second_token)["slides"])
        self.assertEqual(len(self.config()["slides"]), 1)

    def test_api_does_not_expose_a_playlist_owned_by_another_user(self):
        Device.objects.filter(pk=self.foreign_device.pk).update(playlist=self.playlist)
        with self.assertLogs("signage.api", level="ERROR"):
            response = self.config("c" * 43)
        self.assertEqual(response["slides"], [])

    def test_every_playlist_management_surface_rejects_other_owner(self):
        foreign_slide = Slide.objects.create(
            playlist=self.other_playlist, title="Private URL", url="https://example.org",
        )
        for name, kwargs in [
            ("playlist", {}), ("remove-playlist", {}), ("add-slide", {}),
            ("edit-slide", {"slide_id": foreign_slide.pk}),
            ("remove-slide", {"slide_id": foreign_slide.pk}),
        ]:
            with self.subTest(name=name):
                response = self.client.post(self.playlist_url(name, self.other_playlist, **kwargs), {
                    "name": "Stolen", "title": "Stolen", "url": "https://example.com",
                    "position": 0, "duration": 1, "confirm": "on",
                })
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get(self.playlist_url(playlist=self.other_playlist)).status_code, 404)
        self.other_playlist.refresh_from_db()
        foreign_slide.refresh_from_db()
        self.assertEqual(self.other_playlist.name, "Private")
        self.assertEqual(foreign_slide.title, "Private URL")

    def test_slide_cannot_be_edited_through_another_owned_playlist(self):
        another = Playlist.objects.create(owner=self.owner, name="Another")
        for name in ("edit-slide", "remove-slide"):
            self.assertEqual(self.client.post(self.playlist_url(
                name, playlist=another, slide_id=self.slide.pk,
            ), {
                "title": "Changed", "url": "https://example.com", "duration": 1, "position": 0,
            }).status_code, 404)
        self.slide.refresh_from_db()
        self.assertEqual(self.slide.title, "Welcome")

    def test_deletion_requires_confirmation_and_unused_playlist(self):
        url = self.playlist_url("remove-playlist")
        response = self.client.post(url, {"confirm": "on"}, follow=True)
        self.assertContains(response, "before deleting this playlist")
        self.assertTrue(Playlist.objects.filter(pk=self.playlist.pk).exists())
        self.owner.devices.update(playlist=None)
        response = self.client.post(url, follow=True)
        self.assertContains(response, "Confirm deletion")
        self.assertTrue(Playlist.objects.filter(pk=self.playlist.pk).exists())
        self.assertRedirects(self.client.post(url, {"confirm": "on"}), reverse("playlists"))
        self.assertFalse(Slide.objects.filter(pk=self.slide.pk).exists())
        self.assertTrue(Device.objects.filter(pk=self.device.pk).exists())

    def test_removing_display_preserves_shared_playlist_and_playback(self):
        self.client.post(reverse("remove-device", args=[self.device.pk]))
        self.assertTrue(Playlist.objects.filter(pk=self.playlist.pk).exists())
        self.assertEqual([slide["title"] for slide in self.config(self.second_token)["slides"]], ["Welcome"])

    def test_resetting_display_preserves_shared_playlist_and_playback(self):
        self.client.post(reverse("reset-player", args=[self.device.pk]), {"confirm": "on"})
        reset = self.device.reset
        response = self.client.post(
            reverse("api-reset-completed", args=[reset.request_id]), HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )
        self.assertEqual(response.json(), {"status": "completed"})
        self.assertEqual(self.config(), {"state": "unpaired", "slides": []})
        self.assertEqual([slide["title"] for slide in self.config(self.second_token)["slides"]], ["Welcome"])

    def test_playlist_mutations_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        for url in [
            reverse("create-playlist"), reverse("create-device-playlist", args=[self.device.pk]),
            self.playlist_url(), self.playlist_url("add-slide"),
            self.playlist_url("edit-slide", slide_id=self.slide.pk),
            reverse("device", args=[self.device.pk]),
        ]:
            self.assertEqual(client.post(url, {"name": "Changed"}).status_code, 403)

    def test_playlist_surfaces_require_login(self):
        self.client.logout()
        for url in [
            reverse("playlists"), reverse("create-playlist"), self.playlist_url(),
            reverse("create-device-playlist", args=[self.device.pk]),
            self.playlist_url("add-slide"), self.playlist_url("remove-playlist"),
            self.playlist_url("edit-slide", slide_id=self.slide.pk),
            self.playlist_url("remove-slide", slide_id=self.slide.pk),
        ]:
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_account_deletion_cascades_through_shared_playlists(self):
        self.owner.delete()
        self.assertFalse(Playlist.objects.filter(pk=self.playlist.pk).exists())
        self.assertFalse(Device.objects.filter(pk=self.device.pk).exists())
        self.assertFalse(Slide.objects.filter(pk=self.slide.pk).exists())
        self.assertTrue(Playlist.objects.filter(pk=self.other_playlist.pk).exists())

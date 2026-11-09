from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class PlaylistMigrationTests(TransactionTestCase):
    migrate_from = [("signage", "0006_slide_qr_url")]
    migrate_to = [("signage", "0007_shared_playlists")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.apps = executor.loader.project_state(self.migrate_from).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_legacy_urls_are_combined_per_owner_and_displays_share_default(self):
        User = self.apps.get_model("auth", "User")
        Device = self.apps.get_model("signage", "Device")
        Slide = self.apps.get_model("signage", "Slide")
        owner = User.objects.create(username="owner")
        other = User.objects.create(username="other")
        empty_owner = User.objects.create(username="empty")
        User.objects.create(username="no-displays")
        first = Device.objects.create(owner=owner, name="First", token_digest="1" * 64)
        second = Device.objects.create(owner=owner, name="Second", token_digest="2" * 64)
        empty = Device.objects.create(owner=owner, name="Empty", token_digest="3" * 64)
        foreign = Device.objects.create(owner=other, name="Foreign", token_digest="4" * 64)
        empty_display = Device.objects.create(owner=empty_owner, name="No URLs", token_digest="5" * 64)
        unpaired = Device.objects.create(name="Unpaired", token_digest="6" * 64)
        late = Slide.objects.create(
            device=first, title="Late", url="https://example.com/late", duration=17,
            position=8, show_qr=False, qr_url="https://example.com/qr",
        )
        early = Slide.objects.create(
            device=first, title="Early", url="https://example.com/early", duration=23, position=1,
        )
        duplicate = Slide.objects.create(
            device=second, title="Duplicate URL", url=early.url, duration=31, position=1,
        )
        private = Slide.objects.create(device=foreign, title="Private", url="https://example.org")
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        Playlist = apps.get_model("signage", "Playlist")
        Device = apps.get_model("signage", "Device")
        Slide = apps.get_model("signage", "Slide")
        playlist = Playlist.objects.get(owner_id=owner.pk)
        self.assertEqual(playlist.name, "default")
        self.assertEqual(Playlist.objects.count(), 3)
        for device in (first, second, empty):
            self.assertEqual(Device.objects.get(pk=device.pk).playlist_id, playlist.pk)
        slides = list(Slide.objects.filter(playlist=playlist).order_by("position", "id"))
        self.assertEqual([slide.pk for slide in slides], [early.pk, late.pk, duplicate.pk])
        self.assertEqual([slide.position for slide in slides], [1, 2, 3])
        migrated_late = Slide.objects.get(pk=late.pk)
        self.assertEqual(
            (migrated_late.title, migrated_late.url, migrated_late.duration, migrated_late.show_qr, migrated_late.qr_url),
            (late.title, late.url, late.duration, late.show_qr, late.qr_url),
        )
        private_playlist = Playlist.objects.get(owner_id=other.pk)
        self.assertEqual(private_playlist.name, "default")
        self.assertEqual(Device.objects.get(pk=foreign.pk).playlist_id, private_playlist.pk)
        self.assertEqual(Slide.objects.get(pk=private.pk).playlist_id, private_playlist.pk)
        self.assertEqual(Slide.objects.count(), 4)
        self.assertIsNone(Device.objects.get(pk=unpaired.pk).playlist_id)
        self.assertEqual(
            Device.objects.get(pk=empty_display.pk).playlist_id,
            Playlist.objects.get(owner_id=empty_owner.pk).pk,
        )
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        OldSlide = old_apps.get_model("signage", "Slide")
        for device in (first, second, empty):
            self.assertEqual(
                list(OldSlide.objects.filter(device_id=device.pk).values_list("title", flat=True)),
                ["Early", "Late", "Duplicate URL"],
            )
            copied = OldSlide.objects.get(device_id=device.pk, title="Late")
            self.assertEqual((copied.duration, copied.show_qr, copied.qr_url), (17, False, late.qr_url))
        self.assertEqual(OldSlide.objects.filter(device_id=foreign.pk).count(), 1)
        self.assertFalse(OldSlide.objects.filter(device_id=unpaired.pk).exists())
        self.assertFalse(OldSlide.objects.filter(device_id=empty_display.pk).exists())

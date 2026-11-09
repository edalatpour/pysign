import hashlib
import io
import subprocess
import tempfile
import uuid
import zipfile
import shutil
from unittest import skipUnless
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from player.client import validate_config
from signage.assets import content_response, save_content
from signage.models import Asset, Device, Playlist, Slide
from signage.security import digest_token


def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(output, format="PNG")
    return output.getvalue()


@override_settings(SECURE_SSL_REDIRECT=False, ASSET_AZURE_ACCOUNT_URL="")
class AssetTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        override = override_settings(ASSET_STORAGE_ROOT=Path(temporary.name))
        override.enable()
        self.addCleanup(override.disable)
        self.root = Path(temporary.name)
        self.owner = get_user_model().objects.create_user("owner")
        self.other = get_user_model().objects.create_user("other")
        self.playlist = Playlist.objects.create(owner=self.owner, name="Lobby")
        self.token = "a" * 43
        self.device = Device.objects.create(
            owner=self.owner, playlist=self.playlist, token_digest=digest_token(self.token),
        )
        self.client.force_login(self.owner)

    def upload(self, name="Poster", content=None, filename="poster.png"):
        return self.client.post(reverse("assets"), {
            "name": name, "file": SimpleUploadedFile(filename, image_bytes() if content is None else content),
        })

    def create_asset(self):
        self.assertEqual(self.upload().status_code, 302)
        return self.owner.assets.get()

    def content(self, asset, **kwargs):
        return self.client.get(reverse("asset-content", args=[asset.pk]), **kwargs)

    def player_content(self, asset, token=None, **kwargs):
        return self.client.get(
            reverse("api-asset", args=[asset.pk]),
            HTTP_AUTHORIZATION=f"Bearer {token or self.token}", **kwargs,
        )

    def body(self, response):
        return b"".join(response.streaming_content) if response.streaming else response.content

    def test_upload_is_private_and_existing_urls_are_not_changed(self):
        asset = self.create_asset()
        self.assertEqual(asset.owner, self.owner)
        self.assertEqual(asset.content_type, "image/png")
        content = (self.root / asset.storage_key).read_bytes()
        self.assertEqual(asset.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(asset.size, len(content))
        self.assertEqual((self.root / asset.storage_key).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.body(self.content(asset)), content)
        self.assertContains(self.client.get(reverse("assets")), "Poster")
        self.assertEqual(self.client.get("/media/" + asset.storage_key).status_code, 404)
        self.assertEqual(self.client.get("/static/" + asset.storage_key).status_code, 404)

    def test_rejects_html_svg_renamed_files_empty_and_corrupt_media(self):
        for filename, content in [
            ("page.html", b"<h1>Hi</h1>"), ("image.svg", b"<svg></svg>"),
            ("image.png", b"<script>alert(1)</script>"), ("video.mp4", b"not a video"),
            ("empty.png", b""),
        ]:
            with self.subTest(filename=filename):
                response = self.upload(content=content, filename=filename)
                self.assertEqual(response.status_code, 200)
                self.assertIn("file", response.context["form"].errors)
        self.assertFalse(Asset.objects.exists())

    def test_raster_images_are_reencoded_and_exif_orientation_is_preserved(self):
        self.assertEqual(self.upload(content=image_bytes() + b"<script>untrusted</script>").status_code, 302)
        asset = self.owner.assets.get()
        self.assertNotIn(b"<script>", (self.root / asset.storage_key).read_bytes())
        output = io.BytesIO()
        image = Image.new("RGB", (2, 3), "red")
        exif = Image.Exif()
        exif[274] = 6
        image.save(output, "JPEG", exif=exif)
        self.assertEqual(self.upload("Rotated", output.getvalue(), "photo.jpg").status_code, 302)
        rotated = self.owner.assets.get(name="Rotated")
        with Image.open(self.root / rotated.storage_key) as result:
            self.assertEqual(result.size, (3, 2))
            self.assertNotIn(274, result.getexif())

    @override_settings(ASSET_MAX_BYTES=10)
    def test_upload_limit_is_enforced_while_receiving(self):
        self.assertEqual(self.upload(content=b"x" * 11).status_code, 400)
        self.assertFalse(Asset.objects.exists())

    def test_quota_is_owner_scoped(self):
        asset = self.create_asset()
        with override_settings(ASSET_QUOTA_BYTES=asset.size):
            response = self.upload("Second")
            self.assertContains(response, "quota would be exceeded")
            self.assertEqual(self.owner.assets.count(), 1)
            self.client.force_login(self.other)
            self.assertEqual(self.upload("Other user's asset").status_code, 302)
        self.assertEqual(self.other.assets.count(), 1)

    def test_management_and_document_pages_are_owner_scoped(self):
        asset = self.create_asset()
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse("assets")), asset.name)
        for url in [
            reverse("asset-content", args=[asset.pk]),
            reverse("asset-preview", args=[asset.pk, 1]),
            reverse("asset-page", args=[asset.pk, 1]),
        ]:
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(reverse("remove-asset", args=[asset.pk]), {"confirm": "on"}).status_code, 404)
        self.assertTrue((self.root / asset.storage_key).is_file())
        self.client.logout()
        self.assertEqual(self.content(asset).status_code, 302)
        self.assertEqual(self.client.get(reverse("api-asset", args=[asset.pk])).status_code, 401)

    def test_asset_is_reusable_and_only_assigned_owned_players_can_download(self):
        asset = self.create_asset()
        self.assertEqual(self.player_content(asset).status_code, 404)
        data = {"title": "Poster", "asset": str(asset.pk), "duration": 12, "position": 0}
        self.assertEqual(self.client.post(reverse("add-slide", args=[self.playlist.pk]), data).status_code, 302)
        second = Playlist.objects.create(owner=self.owner, name="Second")
        self.assertEqual(self.client.post(reverse("add-slide", args=[second.pk]), data).status_code, 302)
        self.assertEqual(asset.slides.count(), 2)
        self.assertEqual(self.player_content(asset).status_code, 200)
        Device.objects.create(owner=self.other, playlist=self.playlist, token_digest=digest_token("b" * 43))
        self.assertEqual(self.player_content(asset, token="b" * 43).status_code, 404)
        own_unassigned = Device.objects.create(owner=self.owner, token_digest=digest_token("c" * 43))
        self.assertEqual(self.player_content(asset, token="c" * 43).status_code, 404)
        own_unassigned.owner = None
        own_unassigned.save()
        self.assertEqual(self.player_content(asset, token="c" * 43).status_code, 404)
        config = self.client.get(reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {self.token}").json()
        self.assertEqual(validate_config(config), config)
        self.assertEqual(config["slides"][0]["asset"]["id"], str(asset.pk))
        self.assertEqual(config["slides"][0]["url"], "")
        self.device.playlist = None
        self.device.save()
        self.assertEqual(self.player_content(asset).status_code, 404)

    def test_foreign_asset_and_ambiguous_source_and_implicit_qr_are_rejected(self):
        asset = self.create_asset()
        foreign = Playlist.objects.create(owner=self.other, name="Foreign")
        self.client.force_login(self.other)
        data = {"title": "Poster", "asset": str(asset.pk), "duration": 12, "position": 0}
        response = self.client.post(reverse("add-slide", args=[foreign.pk]), data)
        self.assertIn("asset", response.context["form"].errors)
        self.client.force_login(self.owner)
        for extra in [{"url": "https://example.com"}, {"show_qr": "on"}, {"asset": ""}]:
            response = self.client.post(reverse("add-slide", args=[self.playlist.pk]), {**data, **extra})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors)
        self.assertFalse(Slide.objects.exists())
        self.assertEqual(self.client.post(reverse("add-slide", args=[self.playlist.pk]), {
            **data, "show_qr": "on", "qr_url": "https://example.com",
        }).status_code, 302)

    def test_api_filters_corrupt_cross_owner_asset_references(self):
        asset = self.create_asset()
        Asset.objects.filter(pk=asset.pk).update(owner=self.other)
        Slide.objects.create(playlist=self.playlist, title="Corrupt", asset=asset)
        with self.assertLogs("signage.api", level="ERROR"):
            config = self.client.get(reverse("api-config"), HTTP_AUTHORIZATION=f"Bearer {self.token}").json()
        self.assertEqual(config["slides"], [])
        self.assertEqual(self.player_content(asset).status_code, 404)

    def test_deletion_protects_references_but_playlist_deletion_keeps_asset(self):
        asset = self.create_asset()
        slide = Slide.objects.create(playlist=self.playlist, asset=asset, title="Poster", show_qr=False)
        url = reverse("remove-asset", args=[asset.pk])
        self.client.post(url, {"confirm": "on"})
        self.assertTrue(Asset.objects.filter(pk=asset.pk).exists())
        self.assertTrue((self.root / asset.storage_key).is_file())
        self.device.playlist = None
        self.device.save()
        self.client.post(reverse("remove-playlist", args=[self.playlist.pk]), {"confirm": "on"})
        self.assertFalse(Slide.objects.filter(pk=slide.pk).exists())
        self.assertTrue(Asset.objects.filter(pk=asset.pk).exists())
        self.client.post(url)
        self.assertTrue(Asset.objects.filter(pk=asset.pk).exists())
        self.client.post(url, {"confirm": "on"})
        self.assertFalse(Asset.objects.filter(pk=asset.pk).exists())
        self.assertFalse((self.root / asset.storage_key).exists())

    def test_database_requires_exactly_one_source(self):
        asset = self.create_asset()
        for values in ({}, {"asset": asset, "url": "https://example.com"}):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                Slide.objects.create(playlist=self.playlist, title="Bad", **values)

    def test_authenticated_range_and_head_responses(self):
        asset = self.create_asset()
        Slide.objects.create(playlist=self.playlist, asset=asset, title="Poster", show_qr=False)
        content = (self.root / asset.storage_key).read_bytes()
        for value, expected, span in [
            ("bytes=2-5", content[2:6], f"2-5/{asset.size}"),
            ("bytes=-4", content[-4:], f"{asset.size - 4}-{asset.size - 1}/{asset.size}"),
            ("bytes=4-", content[4:], f"4-{asset.size - 1}/{asset.size}"),
        ]:
            response = self.player_content(asset, HTTP_RANGE=value)
            self.assertEqual(response.status_code, 206)
            self.assertEqual(self.body(response), expected)
            self.assertEqual(response["Content-Range"], "bytes " + span)
            self.assertIn("no-store", response["Cache-Control"])
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        for value in ["bytes=5-2", "bytes=-0", "bytes=9999-", "bytes=0-1,4-5", "bytes=-", "bytes=" + "9" * 5000 + "-"]:
            self.assertEqual(self.player_content(asset, HTTP_RANGE=value).status_code, 416)
        response = self.client.head(reverse("api-asset", args=[asset.pk]), HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Length"], str(asset.size))
        self.assertEqual(response.content, b"")
        self.client.logout()
        self.assertEqual(self.player_content(asset).status_code, 200)

    def test_mutations_require_csrf(self):
        asset = self.create_asset()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(reverse("assets"), {"name": "Bad"}).status_code, 403)
        self.assertEqual(client.post(reverse("remove-asset", args=[asset.pk])).status_code, 403)

    @skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Video integration requires FFmpeg.")
    def test_video_inspection_uses_actual_media_not_client_mime(self):
        with tempfile.TemporaryDirectory() as temporary:
            for suffix, codec in (("mp4", "libx264"), ("webm", "libvpx")):
                with self.subTest(suffix=suffix):
                    path = Path(temporary) / f"clip.{suffix}"
                    subprocess.run([
                        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x48:d=1",
                        "-c:v", codec, "-pix_fmt", "yuv420p", str(path),
                    ], check=True)
                    self.assertEqual(self.upload(suffix, path.read_bytes(), f"clip.{suffix}").status_code, 302)
                    self.assertEqual(self.owner.assets.get(name=suffix).content_type, f"video/{suffix}")

    @override_settings(DOCUMENT_CONVERTER_URL="http://converter.internal")
    def test_document_upload_keeps_only_rendered_pages_and_previews_privately(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("page-1.png", image_bytes())
            archive.writestr("page-2.png", image_bytes())
        response = Mock(status_code=200)
        response.iter_content.return_value = [output.getvalue()]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("requests.Session.post", return_value=response):
            self.assertEqual(self.upload("Slides", b"original office document", "slides.pptx").status_code, 302)
        asset = self.owner.assets.get()
        self.assertEqual(asset.content_type, "application/zip")
        self.assertEqual(asset.page_count, 2)
        self.assertNotIn(b"original office document", (self.root / asset.storage_key).read_bytes())
        self.assertContains(self.client.get(reverse("asset-preview", args=[asset.pk, 1])), "Next page")
        self.assertEqual(self.client.get(reverse("asset-page", args=[asset.pk, 1])).content, image_bytes())
        self.assertEqual(self.client.get(reverse("asset-page", args=[asset.pk, 3])).status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("asset-page", args=[asset.pk, 1])).status_code, 404)

    def test_unconfigured_document_conversion_is_explicit(self):
        with override_settings(DOCUMENT_CONVERTER_URL=""):
            self.assertContains(self.upload(content=b"%PDF-1.4", filename="test.pdf"), "conversion is not configured")

    @override_settings(ASSET_AZURE_ACCOUNT_URL="https://private.blob.core.windows.net")
    def test_azure_storage_uses_private_blob_operations_not_public_urls(self):
        asset = Asset(id=uuid.uuid4(), content_type="image/png", size=3)
        with patch("signage.assets.blob") as get_blob:
            save_content(asset, io.BytesIO(b"png"))
            self.assertFalse(get_blob.return_value.upload_blob.call_args.kwargs["overwrite"])
            request = Mock(method="GET", headers={"Range": "bytes=1-2"})
            get_blob.return_value.download_blob.return_value.chunks.return_value = [b"ng"]
            response = content_response(request, asset)
            self.assertEqual(self.body(response), b"ng")
            get_blob.return_value.download_blob.assert_called_once_with(offset=1, length=2)

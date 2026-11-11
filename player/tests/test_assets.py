import hashlib
import io
import os
import tempfile
import threading
import unittest
import uuid
import zipfile
from email.message import Message
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

from player.assets import prepare_assets, validate_asset
from player.browser import Browser
from player.client import API, APIError, ConfigWorker, CredentialRevoked, validate_config
from player.runtime import Rotation
from player.storage import private_directory, reset_state, write_json


class PlayerAssetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = private_directory(Path(temporary.name))
        self.asset = {
            "id": str(uuid.uuid4()), "size": 3, "sha256": hashlib.sha256(b"png").hexdigest(),
            "content_type": "image/png", "page_count": 0,
        }
        self.slide = {
            "title": "Poster", "url": "", "duration": 12, "show_qr": False, "qr_url": "", "asset": self.asset,
        }
        self.config = {"state": "paired", "slides": [self.slide]}
        self.stopping = threading.Event()
        self.target = private_directory(self.state / "assets" / self.asset["id"]) / "content"

    def api(self, body=b"png", status=200, content_type="image/png"):
        api = API("https://signage.example.com")
        response = io.BytesIO(body)
        response.status = status
        response.headers = Message()
        response.headers["Content-Type"] = content_type
        api.opener = Mock()
        api.opener.open.return_value = response
        return api

    def test_download_is_authenticated_atomic_and_validated(self):
        api = self.api()
        api.download_asset(self.asset, "device-secret", self.target, self.stopping)
        self.assertEqual(self.target.read_bytes(), b"png")
        if os.name != "nt":
            self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        request = api.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer device-secret")
        self.assertEqual(request.full_url, f"https://signage.example.com/api/device/assets/{self.asset['id']}/")
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_size_hash_type_status_and_revocation_fail_closed(self):
        for api in [
            self.api(b"bad"), self.api(b"shorter"), self.api(content_type="text/html"),
            self.api(status=206),
        ]:
            with self.assertRaises(APIError):
                api.download_asset(self.asset, "secret", self.target, self.stopping)
            self.assertFalse(self.target.exists())
            self.assertEqual(list(self.target.parent.iterdir()), [])
        api = self.api()
        api.opener.open.side_effect = HTTPError("https://example.com", 401, "revoked", {}, None)
        with self.assertRaises(CredentialRevoked):
            api.download_asset(self.asset, "secret", self.target, self.stopping)

    def test_config_rejects_malformed_assets_and_private_qr_urls(self):
        self.assertEqual(validate_config(self.config), self.config)
        for extra in [
            {"id": "../credentials.json"}, {"sha256": "bad"}, {"size": True},
            {"content_type": "text/html"}, {"page_count": 1},
        ]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_asset({**self.asset, **extra})
        for extra in [{"url": "https://example.com"}, {"show_qr": True}]:
            with self.assertRaises(ValueError):
                validate_config({"state": "paired", "slides": [{**self.slide, **extra}]})

    def test_cache_reuses_immutable_assets_and_prunes_old_assignments(self):
        api = self.api()
        prepare_assets(self.config, api, "secret", self.state, self.stopping)
        prepare_assets(self.config, api, "secret", self.state, self.stopping)
        self.assertEqual(api.opener.open.call_count, 1)
        prepare_assets({"slides": []}, api, "secret", self.state, self.stopping)
        self.assertFalse(self.target.exists())

    def test_reassignment_applies_even_when_the_new_download_is_unavailable(self):
        stale = private_directory(self.state / "assets" / str(uuid.uuid4())) / "content"
        stale.write_bytes(b"old owner content")
        write_json(self.state / "credentials.json", {"server": "https://example.com", "token": "a" * 43})
        worker = ConfigWorker("https://example.com", self.state, "Pi")
        worker.api = Mock()
        worker.api.call.return_value = self.config
        worker.api.download_asset.side_effect = APIError("Download unavailable")
        worker.stopping.wait = Mock(side_effect=lambda _: worker.stopping.set())
        worker.run()
        self.assertEqual(worker.updates.get_nowait(), ("config", self.config))
        self.assertFalse(stale.exists())

    def test_document_extraction_uses_only_expected_page_names(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("page-1.png", b"page 1")
            archive.writestr("page-2.png", b"page 2")
        package = output.getvalue()
        asset = {
            **self.asset, "content_type": "application/zip", "page_count": 2,
            "size": len(package), "sha256": hashlib.sha256(package).hexdigest(),
        }
        prepare_assets(
            {"slides": [{**self.slide, "asset": asset}]}, self.api(package, content_type="application/zip"),
            "secret", self.state, self.stopping,
        )
        self.assertEqual((self.target.parent / "page-2.png").read_bytes(), b"page 2")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("../credentials.json", b"stolen")
        self.target.write_bytes(output.getvalue())
        (self.target.parent / "page-1.png").unlink()
        with self.assertRaises(ValueError):
            prepare_assets(
                {"slides": [{**self.slide, "asset": {**asset, "size": self.target.stat().st_size}}]},
                Mock(), "secret", self.state, self.stopping,
            )
        self.assertFalse((self.state / "credentials.json").exists())

    def test_document_pages_use_the_playlist_entry_duration(self):
        slide = {**self.slide, "asset": {**self.asset, "page_count": 3}}
        rotation = Rotation()
        rotation.configure([slide, {"title": "Next", "duration": 1}])
        self.assertEqual(rotation.due(0), slide)
        self.assertIsNone(rotation.due(35.999))
        self.assertEqual(rotation.due(36)["title"], "Next")

    def test_rendered_wrappers_fit_content_and_keep_credentials_out(self):
        self.target.write_bytes(b"png")
        browser = Browser(self.state, executable="/fake/chromium")
        browser.navigate = Mock()
        browser.navigate_asset(self.asset, 12)
        wrapper = self.target.parent / "display.html"
        self.assertIn("object-fit:contain", wrapper.read_text())
        browser.navigate.assert_called_with(wrapper.as_uri(), show_qr=False, qr_url="")
        browser.navigate_asset({**self.asset, "content_type": "video/mp4"}, 12)
        self.assertIn("autoplay muted loop", wrapper.read_text())
        for number in (1, 2):
            (self.target.parent / f"page-{number}.png").write_bytes(b"png")
        browser.navigate_asset({**self.asset, "content_type": "application/zip", "page_count": 2}, 12)
        self.assertIn("page-2.png", wrapper.read_text())
        self.assertIn("12000", wrapper.read_text())
        self.assertNotIn("Bearer", wrapper.read_text())

    def test_reset_unpair_and_revocation_remove_cached_assets(self):
        self.target.write_bytes(b"png")
        reset_state(self.state)
        self.assertFalse(self.target.exists())
        for response, error in [
            ({"state": "unpaired", "slides": []}, None),
            (None, CredentialRevoked("revoked")),
        ]:
            self.target.parent.mkdir(parents=True, exist_ok=True)
            self.target.write_bytes(b"png")
            write_json(self.state / "credentials.json", {"server": "https://example.com", "token": "a" * 43})
            worker = ConfigWorker("https://example.com", self.state, "Pi")
            worker.api = Mock()
            worker.api.call.side_effect = error or [response, {"code": "ABCDE12345"}]
            worker.stopping.wait = Mock(side_effect=lambda _: worker.stopping.set())
            worker.run()
            self.assertFalse(self.target.exists())
            self.assertIn(worker.updates.get_nowait()[0], ("config", "fatal"))

    def test_unpairing_clears_display_even_if_pairing_code_request_fails(self):
        self.target.write_bytes(b"png")
        write_json(self.state / "credentials.json", {"server": "https://example.com", "token": "a" * 43})
        worker = ConfigWorker("https://example.com", self.state, "Pi")
        worker.api = Mock()
        worker.api.call.side_effect = [{"state": "unpaired", "slides": []}, APIError("No pairing service")]
        worker.stopping.wait = Mock(side_effect=lambda _: worker.stopping.set())
        worker.run()
        self.assertEqual(worker.updates.get_nowait(), ("config", {"state": "unpaired", "slides": []}))
        self.assertFalse(self.target.exists())

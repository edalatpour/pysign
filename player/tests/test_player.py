import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from player.__main__ import install_autostart, parser
from player.browser import Browser
from player.client import API, APIError, ConfigWorker, CredentialRevoked, NoRedirects, server_url, validate_config
from player.runtime import Rotation, cached_config, run_player
from player.storage import private_directory, read_json, write_json


def playlist():
    return {"state": "paired", "slides": [
        {"title": "Scoreboard", "url": "https://example.com/scores", "duration": 5},
        {"title": "Calendar", "url": "https://example.com/calendar", "duration": 5},
    ]}


class RotationTests(unittest.TestCase):
    def test_exact_five_second_cycle_and_repeat(self):
        rotation = Rotation()
        slides = playlist()["slides"]
        rotation.configure(slides)
        self.assertEqual(rotation.due(0), slides[0]["url"])
        self.assertIsNone(rotation.due(4.999))
        self.assertEqual(rotation.due(5), slides[1]["url"])
        self.assertIsNone(rotation.due(9.999))
        self.assertEqual(rotation.due(10), slides[0]["url"])

    def test_variable_durations_and_playlist_updates(self):
        rotation = Rotation()
        slides = playlist()["slides"]
        slides[1]["duration"] = 17
        rotation.configure(slides)
        rotation.due(0)
        rotation.due(5)
        self.assertIsNone(rotation.due(21.999))
        self.assertEqual(rotation.due(22), slides[0]["url"])
        rotation.configure([slides[1]])
        self.assertEqual(rotation.due(23), slides[1]["url"])
        rotation.configure([])
        self.assertIsNone(rotation.due(100))

    def test_identical_poll_does_not_restart_playlist(self):
        rotation = Rotation()
        slides = playlist()["slides"]
        rotation.configure(slides)
        rotation.due(0)
        rotation.configure(playlist()["slides"])
        self.assertIsNone(rotation.due(4))
        self.assertEqual(rotation.due(5), slides[1]["url"])


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = private_directory(Path(self.temporary.name) / "state")
        self.server = "https://signage.example.com"

    def test_storage_is_private_and_atomic(self):
        path = self.state / "credentials.json"
        write_json(path, {"token": "secret"})
        self.assertEqual(read_json(path), {"token": "secret"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["credentials.json"])

    def test_http_and_credential_urls_are_rejected(self):
        for value in ["http://example.com", "https://user:secret@example.com", "file:///tmp", "https://example.com/path"]:
            with self.assertRaises(ValueError):
                server_url(value)
        self.assertEqual(server_url("http://localhost:8000/", allow_http=True), "http://localhost:8000")

    def test_config_validation_rejects_unsafe_urls_and_duration(self):
        for url, duration in [
            ("file:///etc/passwd", 5), ("javascript:alert(1)", 5),
            ("https://user:secret@example.com", 5), ("https://example.com", True),
            ("https://example.com", 0), ("https://example.com", 86401),
        ]:
            with self.subTest(url=url, duration=duration):
                with self.assertRaises(ValueError):
                    validate_config({"state": "paired", "slides": [{"title": "Bad", "url": url, "duration": duration}]})
        self.assertEqual(validate_config(playlist()), playlist())

    def test_credentials_are_bound_to_server(self):
        write_json(self.state / "credentials.json", {"server": "https://other.example.com", "token": "a" * 43})
        worker = ConfigWorker(self.server, self.state, "Pi")
        worker.api = Mock()
        with self.assertRaises(ValueError):
            worker.credentials()
        worker.api.call.assert_not_called()

    def test_offline_cache_is_server_bound(self):
        write_json(self.state / "playlist.json", {"server": self.server, "config": playlist()})
        self.assertEqual(cached_config(self.state, self.server), playlist())
        with self.assertRaises(ValueError):
            cached_config(self.state, "https://other.example.com")

    def test_existing_credentials_are_reused(self):
        worker = ConfigWorker(self.server, self.state, "Pi")
        worker.api = Mock()
        worker.api.call.return_value = {"token": "a" * 43}
        self.assertEqual(worker.credentials(), "a" * 43)
        self.assertEqual(worker.credentials(), "a" * 43)
        self.assertEqual(worker.api.call.call_count, 1)

    def test_poll_caches_valid_config(self):
        worker = ConfigWorker(self.server, self.state, "Pi")
        write_json(self.state / "credentials.json", {"server": self.server, "token": "a" * 43})
        worker.api = Mock()
        worker.api.call.return_value = playlist()
        worker.stopping.wait = Mock(side_effect=lambda _: worker.stopping.set())
        worker.run()
        self.assertEqual(worker.updates.get_nowait(), ("config", playlist()))
        self.assertEqual(cached_config(self.state, self.server), playlist())

    def test_outage_preserves_cached_playlist(self):
        write_json(self.state / "playlist.json", {"server": self.server, "config": playlist()})
        worker = ConfigWorker(self.server, self.state, "Pi")
        worker.api = Mock()
        worker.api.call.side_effect = APIError("Offline")
        worker.stopping.wait = Mock(side_effect=lambda _: worker.stopping.set())
        worker.run()
        self.assertEqual(worker.updates.get_nowait()[0], "offline")
        self.assertEqual(cached_config(self.state, self.server), playlist())

    def test_revocation_removes_cache_and_does_not_reregister(self):
        write_json(self.state / "credentials.json", {"server": self.server, "token": "a" * 43})
        write_json(self.state / "playlist.json", {"server": self.server, "config": playlist()})
        worker = ConfigWorker(self.server, self.state, "Pi")
        worker.api = Mock()
        worker.api.call.side_effect = CredentialRevoked("Revoked")
        worker.run()
        self.assertEqual(worker.updates.get_nowait()[0], "fatal")
        self.assertFalse((self.state / "playlist.json").exists())
        self.assertTrue((self.state / "credentials.json").exists())
        self.assertEqual(worker.api.call.call_count, 1)

    def test_api_refuses_redirects(self):
        handler = NoRedirects()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_http_errors_and_unavailability_are_explicit(self):
        api = API(self.server)
        for code, error_class in [(401, CredentialRevoked), (403, APIError), (500, APIError)]:
            with patch.object(api.opener, "open", side_effect=HTTPError(self.server, code, "", {}, None)):
                with self.assertRaises(error_class):
                    api.call("/api/device/config/", token="a" * 43)
        with patch.object(api.opener, "open", side_effect=URLError("offline")):
            with self.assertRaises(APIError):
                api.call("/api/device/config/")

    def test_default_state_directory_and_branding(self):
        args = parser().parse_args(["run"])
        self.assertEqual(args.state_dir, Path.home() / ".local/state/pisign")
        self.assertIn("PiSign Raspberry Pi Chromium player", parser().format_help())

    def test_autostart_preserves_virtualenv_and_labwc_configuration(self):
        home = Path(self.temporary.name)
        labwc = home / ".config/labwc"
        labwc.mkdir(parents=True)
        (labwc / "autostart").write_text("existing-command &\n")
        args = parser().parse_args(["install-autostart", "--server", self.server, "--state-dir", str(self.state)])
        with patch("player.__main__.Path.home", return_value=home):
            with patch("player.__main__.sys.executable", "/opt/pisign/.venv/bin/python"):
                install_autostart(args, self.server)
                install_autostart(args, self.server)
        desktop = (home / ".config/autostart/pisign.desktop").read_text()
        self.assertIn("Name=PiSign Player", desktop)
        self.assertIn("/opt/pisign/.venv/bin/python", desktop)
        autostart = (labwc / "autostart").read_text()
        self.assertIn("existing-command &", autostart)
        self.assertEqual(autostart.count("start-player"), 1)
        self.assertEqual((self.state / "start-player").stat().st_mode & 0o777, 0o700)

    def test_worker_stops_cleanly(self):
        worker = ConfigWorker(self.server, self.state, "Pi")
        worker.stopping.set()
        worker.start()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())

    def test_running_player_applies_changed_url_and_duration_on_poll(self):
        initial = playlist()
        changed = playlist()
        changed["slides"][0] = {
            "title": "New scoreboard", "url": "https://example.com/new", "duration": 8,
        }
        write_json(self.state / "playlist.json", {"server": self.server, "config": initial})
        worker = Mock()
        worker.updates.get_nowait.side_effect = [
            ("config", initial), ("config", changed), ("config", changed), ("config", changed),
        ]
        browser = Mock()
        browser.process.poll.return_value = None
        ticks = [0, 1, 8.999, 9]
        with patch("player.runtime.time.monotonic", side_effect=ticks):
            with patch("player.runtime.time.sleep", side_effect=[None, None, None, KeyboardInterrupt]):
                with self.assertRaises(KeyboardInterrupt):
                    run_player(browser, worker, self.state, self.server)
        self.assertEqual([call.args[0] for call in browser.navigate.call_args_list], [
            initial["slides"][0]["url"], changed["slides"][0]["url"], changed["slides"][1]["url"],
        ])
        worker.stopping.set.assert_called_once()
        browser.close.assert_called_once()

    def test_browser_navigates_one_tab_and_recovers_status_after_close(self):
        browser = Browser(self.state, executable="/usr/bin/chromium")
        browser.process = Mock()
        browser.process.poll.return_value = None
        browser.socket = Mock()
        browser.socket.recv.return_value = json.dumps({"id": 1, "result": {}})
        browser.navigate("https://example.com")
        command = json.loads(browser.socket.send.call_args.args[0])
        self.assertEqual(command["method"], "Page.navigate")
        self.assertEqual(command["params"], {"url": "https://example.com"})
        browser.close()
        self.assertIsNone(browser.current_url)
        with patch.object(browser, "navigate") as navigate:
            browser.status("<Code>", "Pair & sign in")
            browser.current_url = (self.state / "status.html").as_uri()
            browser.close()
            browser.status("<Code>", "Pair & sign in")
        self.assertEqual(navigate.call_count, 2)
        status = (self.state / "status.html").read_text()
        self.assertIn("&lt;Code&gt;", status)
        self.assertIn("Pair &amp; sign in", status)
        self.assertIn('alt="PiSign"', status)
        self.assertIn("data:image/svg+xml;base64,", status)

    def test_browser_start_uses_kiosk_and_keeps_sandbox_enabled(self):
        browser = Browser(self.state, executable="/usr/bin/chromium")
        profile = private_directory(self.state / "browser-profile")
        process = Mock()
        process.poll.return_value = None

        def launch(*args, **kwargs):
            (profile / "DevToolsActivePort").write_text("12345\n/devtools/browser/test")
            return process

        response = Mock()
        response.read.return_value = json.dumps([{
            "type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:12345/devtools/page/test",
        }])
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch("player.browser.subprocess.Popen", side_effect=launch) as popen:
            with patch("player.browser.build_opener", return_value=opener):
                with patch("websocket.create_connection", return_value=Mock()):
                    browser.start()
        arguments = popen.call_args.args[0]
        self.assertIn("--kiosk", arguments)
        self.assertIn("--remote-debugging-address=127.0.0.1", arguments)
        self.assertNotIn("--no-sandbox", arguments)
        self.assertEqual(arguments[-1], "about:blank")
        browser.close()

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from player.browser import Browser, BrowserError


class BrowserWatermarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.browser = Browser(Path(self.temporary.name), executable="chromium")
        self.addCleanup(self.browser.close)

    def start_browser(self, error=None):
        profile = self.browser.state_dir / "browser-profile"
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
        socket = Mock()
        reply = {"id": self.browser.counter + 2, "result": {"identifier": "watermark"}}
        if error:
            reply["error"] = {"message": error}
        socket.recv.side_effect = [
            json.dumps({"method": "Page.frameNavigated", "params": {}}),
            json.dumps({"id": self.browser.counter + 1, "result": {}}),
            json.dumps(reply),
        ]
        with patch("player.browser.subprocess.Popen", side_effect=launch):
            with patch("player.browser.build_opener", return_value=opener):
                with patch("websocket.create_connection", return_value=socket):
                    self.browser.start()
        return socket

    def test_watermark_is_registered_before_navigation_in_isolated_world(self):
        socket = self.start_browser()
        command = json.loads(socket.send.call_args.args[0])
        self.assertEqual(command["method"], "Page.addScriptToEvaluateOnNewDocument")
        self.assertEqual(command["params"]["worldName"], "PiSign watermark")
        source = command["params"]["source"]
        self.assertIn('"tag": "svg"', source)
        self.assertIn('window !== window.top', source)
        self.assertIn('["http:", "https:"]', source)
        self.assertNotIn("Page.setBypassCSP", source)
        socket.recv.side_effect = [json.dumps({"id": 3, "result": {}})]
        self.browser.navigate("https://example.com")
        command = json.loads(socket.send.call_args.args[0])
        self.assertEqual(command["method"], "Page.navigate")
        self.assertEqual(command["params"], {"url": "https://example.com"})
        self.assertEqual(self.browser.current_url, "https://example.com")
        self.assertEqual(socket.send.call_count, 3)

    def test_restart_reinstalls_watermark(self):
        first = self.start_browser()
        self.browser.close()
        second = self.start_browser()
        first.close.assert_called_once()
        self.assertEqual(json.loads(second.send.call_args.args[0])["method"],
                         "Page.addScriptToEvaluateOnNewDocument")

    def test_registration_failure_is_explicit_and_closes_browser(self):
        with self.assertRaisesRegex(BrowserError, "Page.addScriptToEvaluateOnNewDocument"):
            self.start_browser(error="Registration failed")
        self.assertIsNone(self.browser.socket)
        self.assertIsNone(self.browser.process)

    def test_navigation_failure_does_not_update_current_url(self):
        socket = self.start_browser()
        socket.recv.side_effect = [json.dumps({"id": 3, "error": {"message": "Rejected"}})]
        with self.assertRaisesRegex(BrowserError, "Page.navigate"):
            self.browser.navigate("https://example.com")
        self.assertIsNone(self.browser.current_url)

    def test_status_page_keeps_existing_branding(self):
        with patch.object(self.browser, "navigate") as navigate:
            self.browser.status("<Code>", "Pair & sign in")
        path = self.browser.state_dir / "status.html"
        navigate.assert_called_once_with(path.as_uri())
        content = path.read_text(encoding="utf-8")
        self.assertIn("&lt;Code&gt;", content)
        self.assertIn("Pair &amp; sign in", content)
        self.assertIn('alt="PiSign"', content)
        self.assertNotIn("pisign-watermark", content)

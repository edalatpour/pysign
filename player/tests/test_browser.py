import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from player.browser import Browser, BrowserError, BrowserTimeout
from player.runtime import run_player


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
            self.launch_arguments = args[0]
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

    def test_kiosk_skips_low_memory_launcher_dialog_without_disabling_sandbox(self):
        self.start_browser()
        self.assertIn("--no-memcheck", self.launch_arguments)
        self.assertIn("--kiosk", self.launch_arguments)
        self.assertNotIn("--no-sandbox", self.launch_arguments)

    def test_software_rendering_is_opt_in(self):
        self.start_browser()
        for flag in ["--disable-gpu", "--disable-gpu-compositing", "--disable-gpu-rasterization",
                     "--ozone-platform=x11"]:
            self.assertNotIn(flag, self.launch_arguments)

    def test_software_rendering_disables_gpu_rasterization_and_keeps_sandbox(self):
        self.browser = Browser(self.browser.state_dir, executable="chromium", software_rendering=True)
        self.addCleanup(self.browser.close)
        self.start_browser()
        for flag in ["--disable-gpu", "--disable-gpu-compositing", "--disable-gpu-rasterization",
                     "--ozone-platform=x11"]:
            self.assertIn(flag, self.launch_arguments)
        self.assertNotIn("--no-sandbox", self.launch_arguments)
        self.assertEqual(self.launch_arguments[-1], "about:blank")

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

    def test_command_timeout_keeps_browser_alive_and_ignores_late_reply(self):
        import websocket

        socket = self.start_browser()
        socket.gettimeout.return_value = 3
        process = self.browser.process
        socket.recv.side_effect = websocket.WebSocketTimeoutException("Connection timed out")
        with self.assertRaisesRegex(BrowserTimeout, "Page.getFrameTree timed out"):
            self.browser._command("Page.getFrameTree", {})
        self.assertIs(self.browser.socket, socket)
        self.assertIs(self.browser.process, process)
        socket.close.assert_not_called()
        process.terminate.assert_not_called()
        self.assertEqual(socket.settimeout.call_args.args, (3,))
        socket.recv.side_effect = [
            json.dumps({"id": 3, "result": {"frameTree": {}}}),
            json.dumps({"method": "Page.frameNavigated", "params": {}}),
            json.dumps({"id": 4, "result": {"frameTree": {"frame": {"url": "https://example.com"}}}}),
        ]
        self.assertEqual(
            self.browser._command("Page.getFrameTree", {}),
            {"frameTree": {"frame": {"url": "https://example.com"}}},
        )

    def test_command_deadline_raises_timeout_without_closing_browser(self):
        socket = self.start_browser()
        with patch("player.browser.time.monotonic", side_effect=[0, 5]):
            with self.assertRaises(BrowserTimeout):
                self.browser._command("Page.getFrameTree", {})
        socket.close.assert_not_called()

    def test_navigation_uses_thirty_second_budget_and_restores_socket_timeout(self):
        socket = self.start_browser()
        socket.gettimeout.return_value = 3
        socket.settimeout.reset_mock()
        socket.recv.side_effect = [
            json.dumps({"method": "Page.frameNavigated", "params": {}}),
            json.dumps({"id": 3, "result": {}}),
        ]
        with patch("player.browser.time.monotonic", side_effect=[0, 4, 14]):
            self.browser.navigate("https://scoreboard.example.com")
        self.assertEqual([call.args for call in socket.settimeout.call_args_list],
                         [(30,), (26,), (16,), (3,)])
        self.assertEqual(self.browser.current_url, "https://scoreboard.example.com")

    def test_connection_failure_still_closes_browser(self):
        import websocket

        socket = self.start_browser()
        socket.recv.side_effect = websocket.WebSocketConnectionClosedException("Closed")
        with self.assertRaisesRegex(BrowserError, "WebSocketConnectionClosedException"):
            self.browser._command("Page.getFrameTree", {})
        self.assertIsNone(self.browser.socket)
        self.assertIsNone(self.browser.process)

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

    def qr_responses(self, url, rendered=True):
        return [
            {"frameTree": {"frame": {"id": "main", "url": url}}},
            {"executionContextId": 7},
            {"result": {"value": url}},
            {"result": {"value": rendered}},
        ]

    def test_qr_uses_actual_url_and_caches_unchanged_content(self):
        self.browser.socket = Mock()
        url = "https://example.com/redirected?query=1#section"
        with patch.object(self.browser, "_command", side_effect=self.qr_responses(url)) as command:
            self.browser.update_watermark()
            expression = command.call_args.args[1]["expression"]
            self.assertIn(json.dumps(url), expression)
            self.assertEqual(command.call_args.args[1]["contextId"], 7)
        self.assertEqual(self.browser.qr_location, (7, url))
        self.browser.qr_checked_at = 0
        with patch.object(self.browser, "_command", side_effect=self.qr_responses(url)[:3]) as command:
            self.browser.update_watermark()
            self.assertEqual(command.call_count, 3)

    def test_qr_rate_limit_and_local_status_exclusion(self):
        self.browser.socket = Mock()
        with patch("player.browser.time.monotonic", return_value=10):
            with patch.object(self.browser, "_command", return_value={
                "frameTree": {"frame": {"id": "main", "url": "file:///status.html"}},
            }) as command:
                self.browser.update_watermark()
                self.browser.update_watermark()
                command.assert_called_once_with("Page.getFrameTree", {})

    def test_qr_overflow_is_logged_and_hidden(self):
        self.browser.socket = Mock()
        url = "https://example.com/?value=" + "a" * 4000
        with patch.object(self.browser, "_command", side_effect=self.qr_responses(url)) as command:
            with self.assertLogs("player.browser", level="WARNING"):
                self.browser.update_watermark()
            self.assertIn(", null)", command.call_args.args[1]["expression"])
        self.assertEqual(self.browser.qr_location, (7, url))

    def test_qr_navigation_race_retries_and_errors_are_explicit(self):
        self.browser.socket = Mock()
        with patch.object(self.browser, "_command",
                          side_effect=self.qr_responses("https://example.com", rendered=False)):
            self.browser.update_watermark()
        self.assertIsNone(self.browser.qr_location)
        self.browser.qr_checked_at = 0
        responses = self.qr_responses("https://example.com")
        responses[-1] = {"exceptionDetails": {"text": "Error"}}
        with patch.object(self.browser, "_command", side_effect=responses):
            with self.assertRaisesRegex(BrowserError, "render the watermark QR"):
                self.browser.update_watermark()

    def test_browser_failure_logs_traceback_and_preserves_retry_cleanup(self):
        worker = Mock()
        config = {"state": "paired", "slides": [
            {"url": "https://example.com", "duration": 30},
        ]}
        worker.updates.get_nowait.return_value = ("config", config)
        browser = Mock()
        error = BrowserTimeout("Chromium Page.navigate timed out.")
        browser.navigate.side_effect = error
        with patch("player.runtime.cached_config", return_value=config):
            with patch("player.runtime.time.sleep", side_effect=[None, KeyboardInterrupt]) as sleep:
                with self.assertLogs("player.runtime", level="ERROR") as logs:
                    with self.assertRaises(KeyboardInterrupt):
                        run_player(browser, worker, self.browser.state_dir, "https://pisign.example.com")
        self.assertEqual(len(logs.records), 1)
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertIs(logs.records[0].exc_info[1], error)
        self.assertIn("Restarting Chromium in five seconds.", logs.records[0].getMessage())
        self.assertIn("Traceback (most recent call last):", logs.output[0])
        self.assertEqual(sleep.call_args_list[0].args, (5,))
        self.assertEqual(browser.close.call_count, 2)
        worker.stopping.set.assert_called_once_with()
        worker.join.assert_called_once_with(timeout=12)

    def test_running_player_refreshes_qr_between_slide_rotations(self):
        worker = Mock()
        config = {"state": "paired", "slides": [
            {"url": "https://example.com", "duration": 30},
        ]}
        worker.updates.get_nowait.return_value = ("config", config)
        browser = Mock()
        browser.process.poll.return_value = None
        with patch("player.runtime.cached_config", return_value=config):
            with patch("player.runtime.time.monotonic", side_effect=[0, 1]):
                with patch("player.runtime.time.sleep", side_effect=[None, KeyboardInterrupt]):
                    with self.assertRaises(KeyboardInterrupt):
                        run_player(browser, worker, self.browser.state_dir, "https://pisign.example.com")
        browser.navigate.assert_called_once_with("https://example.com")
        self.assertEqual(browser.update_watermark.call_count, 2)
        browser.close.assert_called_once()

    def test_watermark_timeout_is_logged_without_restarting_or_stopping_rotation(self):
        worker = Mock()
        config = {"state": "paired", "slides": [
            {"url": "https://example.com", "duration": 1},
            {"url": "https://scoreboard.example.com", "duration": 1},
        ]}
        worker.updates.get_nowait.return_value = ("config", config)
        browser = Mock()
        browser.process.poll.return_value = None
        browser.update_watermark.side_effect = [
            BrowserTimeout("Chromium Page.getFrameTree timed out."), None,
        ]
        with patch("player.runtime.cached_config", return_value=config):
            with patch("player.runtime.time.monotonic", side_effect=[0, 1]):
                with patch("player.runtime.time.sleep", side_effect=[None, KeyboardInterrupt]) as sleep:
                    with self.assertLogs("player.runtime", level="WARNING") as logs:
                        with self.assertRaises(KeyboardInterrupt):
                            run_player(browser, worker, self.browser.state_dir, "https://pisign.example.com")
        self.assertEqual([call.args[0] for call in browser.navigate.call_args_list],
                         ["https://example.com", "https://scoreboard.example.com"])
        self.assertEqual(browser.update_watermark.call_count, 2)
        browser.start.assert_not_called()
        browser.close.assert_called_once()
        self.assertTrue(all(call.args == (0.1,) for call in sleep.call_args_list))
        self.assertIn("Skipping this watermark refresh.", logs.output[0])

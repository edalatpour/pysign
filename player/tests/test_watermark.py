import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import segno

from player.browser import Browser


class ContentHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/content")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; frame-src 'self'; "
                         "require-trusted-types-for 'script'; trusted-types 'none'")
        self.end_headers()
        self.wfile.write(
            b'<!doctype html><title>Content</title><body>'
            b'<h1>Playlist content</h1><button id="button">Content button</button>'
            b'<div id="fullscreen">Fullscreen content</div>'
            b'<iframe src="/frame" title="Embedded content"></iframe></body>'
            if self.path != "/frame" else b'<!doctype html><title>Frame</title><p>Frame content</p>'
        )

    def log_message(self, format, *args):
        pass


@unittest.skipUnless(os.environ.get("PISIGN_TEST_BROWSER"), "Set PISIGN_TEST_BROWSER to Chromium/Chrome")
class WatermarkIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ContentHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.browser = Browser(Path(self.temporary.name), executable=os.environ["PISIGN_TEST_BROWSER"])
        self.addCleanup(self.close_browser)
        popen = subprocess.Popen

        def headless(arguments, **kwargs):
            return popen([*arguments, "--headless=new", "--window-size=1280,720"], **kwargs)

        self.patcher = patch("player.browser.subprocess.Popen", side_effect=headless)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def close_browser(self):
        if self.browser.socket and self.browser.process.poll() is None:
            self.browser.socket.send(json.dumps({
                "id": self.browser.counter + 1, "method": "Browser.close",
            }))
            self.browser.process.wait(timeout=10)
        self.browser.close()

    def evaluate(self, expression):
        result = self.browser._command("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
            "userGesture": True,
        })
        self.assertNotIn("exceptionDetails", result, result)
        return result["result"].get("value")

    def wait_for(self, expression):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.browser.update_watermark()
            if self.evaluate(expression):
                return
            time.sleep(0.05)
        self.fail(f"Timed out: {expression}")

    def open_content(self, path="/content"):
        self.browser.navigate(self.origin + path)
        self.wait_for("document.readyState === 'complete' && !!document.getElementById('pisign-watermark')")
        self.wait_for("document.getElementById('pisign-watermark').dataset.qrUrl === location.href")

    def svg_nodes(self):
        self.browser._command("DOM.enable", {})
        tree = self.browser._command("DOM.getDocument", {"depth": -1, "pierce": True})

        def nodes(node):
            if node.get("localName") == "svg":
                yield node
            for child in node.get("children", []) + node.get("shadowRoots", []):
                yield from nodes(child)

        return list(nodes(tree["root"]))

    def assert_qr(self, url):
        self.wait_for(
            f"document.getElementById('pisign-watermark').dataset.qrUrl === {json.dumps(url)}"
        )
        logo, qr_svg = self.svg_nodes()
        matrix = segno.make(url, error="m", micro=False, encoding="utf-8", eci=True,
                            boost_error=False).matrix
        size = len(matrix) + 8
        attributes = dict(zip(qr_svg["attributes"][::2], qr_svg["attributes"][1::2]))
        self.assertEqual(attributes["viewBox"], f"0 0 {size} {size}")
        path, = qr_svg["children"]
        path_attributes = dict(zip(path["attributes"][::2], path["attributes"][1::2]))
        expected = "".join(
            f"M{x + 4},{y + 4}h1v1h-1z"
            for y, row in enumerate(matrix) for x, dark in enumerate(row) if dark
        )
        self.assertEqual(path_attributes["d"], expected)
        self.assertEqual(path_attributes["fill"], "#000")
        model = self.browser._command("DOM.getBoxModel", {"nodeId": qr_svg["nodeId"]})["model"]
        self.assertEqual(model["width"], max(100, size * 3))
        self.assertEqual(model["height"], model["width"])
        logo_model = self.browser._command("DOM.getBoxModel", {"nodeId": logo["nodeId"]})["model"]
        self.assertAlmostEqual(model["border"][1] - logo_model["border"][5], 8, delta=0.1)
        self.assertAlmostEqual(
            (model["border"][0] + model["border"][2]) / 2,
            (logo_model["border"][0] + logo_model["border"][2]) / 2,
            delta=0.1,
        )
        self.assertIn("background: rgb(255, 255, 255)", attributes["style"])
        self.assertIn("opacity: 0.65", dict(zip(logo["attributes"][::2],
                                               logo["attributes"][1::2]))["style"])

    def test_strict_csp_geometry_and_click_through(self):
        self.open_content()
        metrics = self.evaluate("""(() => {
            const host = document.getElementById('pisign-watermark');
            const rect = host.getBoundingClientRect();
            const style = getComputedStyle(host);
            return {
                width: rect.width, right: innerWidth - rect.right, bottom: innerHeight - rect.bottom,
                opacity: style.opacity, pointerEvents: style.pointerEvents,
                inert: host.inert, shadowClosed: host.shadowRoot === null,
                clickThrough: document.elementFromPoint(rect.x + 5, rect.y + 5) !== host,
            };
        })()""")
        size = len(segno.make(self.origin + "/content", error="m", micro=False,
                              encoding="utf-8", eci=True, boost_error=False).matrix) + 8
        self.assertEqual(metrics, {
            "width": 16 + max(100, size * 3), "right": 16, "bottom": 16,
            "opacity": "1", "pointerEvents": "none",
            "inert": True, "shadowClosed": True, "clickThrough": True,
        })
        self.assert_qr(self.origin + "/content")
        svg, _ = self.svg_nodes()
        model = self.browser._command("DOM.getBoxModel", {"nodeId": svg["nodeId"]})["model"]
        self.assertEqual(model["width"], 100)
        self.assertGreater(model["height"], 30)

    def test_redirect_reload_and_iframe_do_not_duplicate_watermark(self):
        self.open_content("/redirect")
        for reload in (False, True):
            if reload:
                self.evaluate("window.__beforeReload = true")
                self.browser._command("Page.reload", {})
                self.wait_for(
                    "!window.__beforeReload && document.readyState === 'complete' "
                    "&& !!document.querySelector('iframe')"
                )
            self.wait_for("document.querySelectorAll('#pisign-watermark').length === 1")
            self.assertEqual(self.evaluate(
                "document.querySelector('iframe').contentDocument.querySelectorAll('#pisign-watermark').length"
            ), 0)
            self.assertEqual(self.evaluate("location.pathname"), "/content")
            self.assert_qr(self.origin + "/content")

    def test_qr_tracks_history_query_and_fragment_changes(self):
        self.open_content()
        for method, path in (
            ("pushState", "/next?title=caf%C3%A9&mode=show#section"),
            ("replaceState", "/updated?value=%26%3C%3E#second"),
        ):
            self.evaluate(f"history.{method}({{}}, '', {json.dumps(path)})")
            self.assert_qr(self.origin + path)
        self.evaluate("history.back()")
        self.wait_for("location.pathname === '/content'")
        self.assert_qr(self.origin + "/content")
        self.evaluate("location.hash = 'final'")
        self.assert_qr(self.origin + "/content#final")

    def test_qr_grows_for_long_urls(self):
        url = self.origin + "/content?value=" + "a" * 500
        self.open_content("/content?value=" + "a" * 500)
        self.assert_qr(url)

    def test_disabled_qr_keeps_logo_across_redirect_reload_and_history(self):
        with patch("segno.make") as make:
            self.browser.navigate(self.origin + "/redirect", show_qr=False)
            self.wait_for("document.readyState === 'complete' && !!document.getElementById('pisign-watermark')")
            self.assertEqual(len(self.svg_nodes()), 1)
            self.assertIsNone(self.evaluate("document.getElementById('pisign-watermark').dataset.qrUrl"))
            self.evaluate("window.__beforeReload = true")
            self.browser._command("Page.reload", {})
            self.wait_for(
                "!window.__beforeReload && document.readyState === 'complete' "
                "&& !!document.getElementById('pisign-watermark')"
            )
            self.evaluate("history.pushState({}, '', '/content#disabled')")
            self.browser.qr_checked_at = 0
            self.browser.update_watermark()
            self.assertEqual(len(self.svg_nodes()), 1)
            make.assert_not_called()
        self.browser.navigate(self.origin + "/content#enabled", show_qr=True)
        self.assert_qr(self.origin + "/content#enabled")
        with patch("segno.make") as make:
            self.browser.navigate(self.origin + "/content#disabled", show_qr=False)
            self.wait_for("!document.getElementById('pisign-watermark').dataset.qrUrl")
            self.assertEqual(len(self.svg_nodes()), 1)
            make.assert_not_called()

    def test_custom_qr_destination_is_independent_of_page_navigation(self):
        target = "https://example.org/scan?source=display#info"
        self.browser.navigate(self.origin + "/redirect", qr_url=target)
        self.wait_for("document.readyState === 'complete' && !!document.getElementById('pisign-watermark')")
        self.assertEqual(self.evaluate("location.href"), self.origin + "/content")
        self.assert_qr(target)
        self.evaluate("history.pushState({}, '', '/content#next')")
        self.browser.qr_checked_at = 0
        self.browser.update_watermark()
        self.assert_qr(target)
        self.evaluate("window.__beforeReload = true")
        self.browser._command("Page.reload", {})
        self.wait_for(
            "!window.__beforeReload && document.readyState === 'complete' "
            "&& !!document.getElementById('pisign-watermark')"
        )
        self.assert_qr(target)
        replacement = "https://example.org/replacement"
        self.browser.navigate(self.origin + "/content#next", qr_url=replacement)
        self.assert_qr(replacement)
        with patch("segno.make") as make:
            self.browser.navigate(self.origin + "/content#next", show_qr=False, qr_url=replacement)
            self.wait_for("!document.getElementById('pisign-watermark').dataset.qrUrl")
            self.assertEqual(len(self.svg_nodes()), 1)
            make.assert_not_called()
        self.browser.navigate(self.origin + "/content#next")
        self.assert_qr(self.origin + "/content#next")

    def test_removal_body_replacement_and_fullscreen_container(self):
        self.open_content()
        self.evaluate("document.getElementById('pisign-watermark').remove()")
        self.wait_for("document.querySelectorAll('#pisign-watermark').length === 1")
        self.evaluate("document.body.replaceWith(document.createElement('body'))")
        self.assertEqual(self.evaluate("document.querySelectorAll('#pisign-watermark').length"), 1)
        self.evaluate("""(() => {
            const container = document.createElement('div');
            container.id = 'fullscreen';
            document.body.append(container);
            return container.requestFullscreen();
        })()""")
        self.wait_for("document.getElementById('pisign-watermark').parentNode === document.fullscreenElement")
        self.evaluate("document.getElementById('pisign-watermark').remove()")
        self.wait_for("document.fullscreenElement.querySelectorAll('#pisign-watermark').length === 1")
        self.evaluate("document.exitFullscreen()")
        self.wait_for("document.getElementById('pisign-watermark').parentNode === document.documentElement")

    def test_local_status_excluded_and_browser_restart_restores_watermark(self):
        self.open_content()
        self.browser.status("Ready to display", "Your display is paired.")
        self.wait_for("location.protocol === 'file:' && document.readyState === 'complete'")
        self.assertEqual(self.evaluate("document.querySelectorAll('#pisign-watermark').length"), 0)
        self.close_browser()
        self.open_content()
        self.assertEqual(self.evaluate("document.querySelectorAll('#pisign-watermark').length"), 1)

import base64
import html
import json
import logging
import os
import shutil
import subprocess
import time
from importlib.resources import files
from urllib.request import ProxyHandler, build_opener
from xml.etree import ElementTree

from .storage import private_directory


logger = logging.getLogger(__name__)


def watermark_script():
    def element(node):
        return {
            "tag": node.tag.rsplit("}", 1)[-1],
            "attributes": node.attrib,
            "text": node.text or "",
            "children": [element(child) for child in node],
        }

    logo = ElementTree.fromstring(
        files("pisign").joinpath("static/pisign/brand/logo-dark.svg").read_text(encoding="utf-8"),
    )
    script = files("player").joinpath("watermark.js").read_text(encoding="utf-8")
    return f"({script})({json.dumps(element(logo))});"


class BrowserError(Exception):
    pass


class BrowserTimeout(BrowserError):
    pass


class Browser:
    def __init__(self, state_dir, executable=None, software_rendering=False):
        self.state_dir = state_dir
        self.software_rendering = software_rendering
        self.executable = executable or shutil.which("chromium") or shutil.which("chromium-browser")
        if not self.executable:
            raise BrowserError("Install Chromium first, or pass --browser /path/to/chromium.")
        self.process = None
        self.socket = None
        self.counter = 0
        self.current_url = None
        self.status_content = None
        self.qr_checked_at = 0
        self.qr_location = None
        self.show_qr = True
        self.qr_url = ""

    def start(self):
        import websocket

        self.close()
        profile = private_directory(self.state_dir / "browser-profile")
        port_file = profile / "DevToolsActivePort"
        port_file.unlink(missing_ok=True)
        environment = os.environ.copy()
        self.log = (self.state_dir / "chromium.log").open("a", encoding="utf-8")
        try:
            rendering_arguments = [
                "--disable-gpu", "--disable-gpu-compositing", "--disable-gpu-rasterization",
                "--ozone-platform=x11",
            ] if self.software_rendering else []
            self.process = subprocess.Popen([
                self.executable, *rendering_arguments,
                "--no-memcheck", "--kiosk", "--no-first-run", "--no-default-browser-check",
                "--disable-session-crashed-bubble", "--disable-extensions",
                "--autoplay-policy=no-user-gesture-required",
                "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                f"--user-data-dir={profile}", "about:blank",
            ], env=environment, stdout=self.log, stderr=self.log)
            deadline = time.monotonic() + 30
            opener = build_opener(ProxyHandler({}))
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise BrowserError("Chromium exited. See chromium.log; run as a desktop user, not root.")
                if port_file.exists():
                    try:
                        port = int(port_file.read_text().splitlines()[0])
                        with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                            tabs = json.load(response)
                        page = next((tab for tab in tabs if tab.get("type") == "page"), None)
                        if page:
                            self.socket = websocket.create_connection(
                                page["webSocketDebuggerUrl"], timeout=3,
                                suppress_origin=True, http_proxy_host=None,
                            )
                            self._command("Page.enable", {})
                            self._command("Page.addScriptToEvaluateOnNewDocument", {
                                "source": watermark_script(),
                                "worldName": "PiSign watermark",
                            })
                            self.current_url = None
                            return
                    except (OSError, ValueError, IndexError):
                        pass
                time.sleep(0.2)
            raise BrowserError("Timed out connecting to Chromium.")
        except BrowserError:
            self.close()
            raise
        except (OSError, websocket.WebSocketException) as exc:
            self.close()
            raise BrowserError(f"Could not start Chromium: {type(exc).__name__}.") from exc

    def _command(self, method, params, timeout=5):
        import websocket

        self.counter += 1
        socket = self.socket
        previous_timeout = socket.gettimeout()
        deadline = time.monotonic() + timeout
        try:
            socket.settimeout(timeout)
            socket.send(json.dumps({
                "id": self.counter, "method": method, "params": params,
            }))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserTimeout(f"Chromium {method} timed out.")
                socket.settimeout(remaining)
                response = json.loads(socket.recv())
                if response.get("id") == self.counter:
                    if "error" in response:
                        raise BrowserError(f"Chromium rejected {method}.")
                    return response.get("result", {})
        except websocket.WebSocketTimeoutException as exc:
            raise BrowserTimeout(f"Chromium {method} timed out.") from exc
        except (OSError, ValueError, websocket.WebSocketException) as exc:
            self.close()
            raise BrowserError(f"Chromium connection failed: {type(exc).__name__}.") from exc
        finally:
            if self.socket is socket:
                socket.settimeout(previous_timeout)

    def navigate(self, url, show_qr=True, qr_url=""):
        if self.process is None or self.process.poll() is not None or self.socket is None:
            self.start()
        self._command("Page.navigate", {"url": url}, timeout=30)
        self.current_url = url
        self.show_qr = show_qr
        self.qr_url = qr_url
        self.qr_checked_at = 0

    def update_watermark(self):
        now = time.monotonic()
        if self.socket is None or now - self.qr_checked_at < 1:
            return
        self.qr_checked_at = now
        frame = self._command("Page.getFrameTree", {})["frameTree"]["frame"]
        if not frame["url"].startswith(("http://", "https://")):
            return
        context = self._command("Page.createIsolatedWorld", {
            "frameId": frame["id"], "worldName": "PiSign watermark",
        })["executionContextId"]
        result = self._command("Runtime.evaluate", {
            "expression": "location.href", "contextId": context, "returnByValue": True,
        })
        if "exceptionDetails" in result:
            raise BrowserError("Could not read the watermark content URL.")
        url = result["result"]["value"]
        qr_url = self.qr_url or url
        location = (context, url, self.show_qr, qr_url)
        if self.qr_location == location:
            return
        matrix = None
        if self.show_qr:
            import segno

            try:
                qr = segno.make(qr_url, error="m", micro=False, encoding="utf-8", eci=True,
                                boost_error=False)
                matrix = [list(row) for row in qr.matrix]
            except segno.DataOverflowError:
                logger.warning("QR URL exceeds QR capacity; hiding the watermark QR code.")
        result = self._command("Runtime.evaluate", {
            "expression": (
                "typeof globalThis.__pisignUpdateQr === 'function' && "
                f"globalThis.__pisignUpdateQr({json.dumps(url)}, {json.dumps(matrix)}, {json.dumps(qr_url)})"
            ),
            "contextId": context, "returnByValue": True,
        })
        if "exceptionDetails" in result:
            raise BrowserError("Could not render the watermark QR code.")
        if result["result"].get("value"):
            self.qr_location = location

    def status(self, heading, message):
        content = (heading, message)
        path = self.state_dir / "status.html"
        if content != self.status_content:
            logo = base64.b64encode(
                files("pisign").joinpath("static/pisign/brand/logo-dark.svg").read_bytes()
            ).decode("ascii")
            path.write_text(
                '<!doctype html><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>PiSign</title>'
                '<style>*{box-sizing:border-box}body{margin:0;padding:5vh 6vw;'
                'background:#142b32;color:#f4f8f7;font:clamp(18px,2.4vw,28px) system-ui;'
                'display:grid;place-content:center;min-height:100vh;text-align:center}'
                '.brand{display:block;width:min(440px,65vw);height:auto;margin:0 auto 4vh}'
                'h1{font-size:clamp(32px,5vw,64px);overflow-wrap:anywhere;margin:0 0 1rem}'
                'p{max-width:1000px;white-space:pre-line;overflow-wrap:anywhere;color:#b4c9c7}</style>'
                f'<img class="brand" src="data:image/svg+xml;base64,{logo}" alt="PiSign">'
                f"<h1>{html.escape(heading)}</h1><p>{html.escape(message)}</p>",
                encoding="utf-8",
            )
            path.chmod(0o600)
        url = path.as_uri()
        if content != self.status_content or self.current_url != url:
            self.navigate(url)
        self.status_content = content

    def close(self):
        self.current_url = None
        self.qr_checked_at = 0
        self.qr_location = None
        if self.socket:
            self.socket.close()
            self.socket = None
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None
        if getattr(self, "log", None):
            self.log.close()

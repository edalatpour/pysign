import json
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent

from player.storage import private_directory, read_json, write_json


HARNESS = """
import json
import os
import sys
from pathlib import Path
import player.__main__ as entry
from player.client import ConfigWorker

state = Path(sys.argv[sys.argv.index("--state-dir") + 1])

def event(kind):
    with (state / "events.jsonl").open("a") as output:
        output.write(json.dumps({"kind": kind, "pid": os.getpid(), "argv": sys.argv}) + "\\n")

class Process:
    def poll(self):
        return None

class Browser:
    def __init__(self, *args, **kwargs):
        self.process = None

    def start(self):
        self.process = Process()
        event("browser-started")

    def status(self, *args):
        if self.process is None:
            self.start()

    def close(self):
        self.process = None
        event("browser-closed")

entry.Browser = Browser
entry.ConfigWorker = lambda *args: ConfigWorker(*args, interval=0.05)
event("launch")
entry.main()
"""


class RestartIntegrationTests(unittest.TestCase):
    def test_real_reexec_reconnects_and_deduplicates_replayed_command(self):
        with tempfile.TemporaryDirectory() as directory:
            state = private_directory(Path(directory) / "state")
            request_id = str(uuid.uuid4())
            command = {
                "id": request_id,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            }
            acknowledged = threading.Event()
            replayed = threading.Event()
            acknowledgements = []
            replay_count = 0

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def respond(self, data, status=200):
                    body = json.dumps(data).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    nonlocal replay_count
                    if self.path != "/api/device/config/" or self.headers.get("Authorization") != "Bearer " + "a" * 43:
                        self.respond({"error": "Unauthorized"}, 401)
                        return
                    config = {"state": "paired", "name": "Lobby", "slides": []}
                    if "browser-started" in (state / "events.jsonl").read_text():
                        config["restart"] = command
                    self.respond(config)
                    if acknowledged.is_set():
                        replay_count += 1
                        if replay_count >= 4:
                            replayed.set()

                def do_POST(self):
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    if self.path != f"/api/device/restarts/{request_id}/completed/":
                        self.respond({"error": "Unknown command"}, 404)
                        return
                    events = [json.loads(line) for line in (state / "events.jsonl").read_text().splitlines()]
                    acknowledgements.append((self.headers.get("Authorization"), events))
                    if len(acknowledgements) == 1:
                        self.respond({"error": "Temporary outage"}, 503)
                    else:
                        self.respond({"status": "completed"})
                        acknowledged.set()

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            credentials = {"server": origin, "token": "a" * 43}
            write_json(state / "credentials.json", credentials)
            cookies = private_directory(state / "browser-profile") / "Cookies"
            cookies.write_bytes(b"existing browser cookies")
            script = Path(directory) / "player_harness.py"
            script.write_text(dedent(HARNESS))
            arguments = [
                "run", "--server", origin, "--allow-http", "--state-dir", str(state),
                "--name", "Lobby", "--software-rendering", "--browser", "/opt/chromium",
            ]
            process = subprocess.Popen(
                [sys.executable, "-u", str(script), *arguments],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            try:
                self.assertTrue(replayed.wait(timeout=8), "Restart did not reconnect and acknowledge.")
                self.assertIsNone(process.poll(), "Player exited instead of continuing playback.")
                events = [json.loads(line) for line in (state / "events.jsonl").read_text().splitlines()]
                launches = [event for event in events if event["kind"] == "launch"]
                self.assertEqual(len(launches), 2)
                self.assertEqual([event["pid"] for event in launches], [process.pid, process.pid])
                self.assertEqual([event["argv"][1:] for event in launches], [arguments, arguments])
                kinds = [event["kind"] for event in events]
                self.assertEqual(kinds, [
                    "launch", "browser-started", "browser-closed", "launch", "browser-started",
                ])
                for authorization, at_ack in acknowledgements:
                    self.assertEqual(authorization, "Bearer " + "a" * 43)
                    self.assertEqual(sum(event["kind"] == "launch" for event in at_ack), 2)
                    self.assertEqual(sum(event["kind"] == "browser-started" for event in at_ack), 2)
                self.assertEqual(len(acknowledgements), 2)
                self.assertEqual(read_json(state / "restart.json"), {
                    "server": origin, "id": request_id, "acknowledged": True,
                })
                self.assertEqual(read_json(state / "credentials.json"), credentials)
                self.assertEqual(cookies.read_bytes(), b"existing browser cookies")
                self.assertNotIn("restart", read_json(state / "playlist.json")["config"])
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    output, _ = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output, _ = process.communicate()
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)
                if not replayed.is_set():
                    print(output.decode())

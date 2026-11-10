import base64
import io
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from player.client import ConfigWorker
from player.__main__ import main
from player.releases import canonical, verify
from player.storage import read_json, write_json
from player.updater import Supervisor, bootstrap, extract_release


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "updater"
        self.state = self.root / "state"
        self.home.mkdir()
        self.state.mkdir()
        self.private = self.root / "private.pem"
        self.public = self.home / "release-public.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "Ed25519", "-out", str(self.private)],
                       check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", str(self.private), "-pubout", "-out", str(self.public)],
                       check=True, capture_output=True)
        self.manifest = {
            "sequence": 1, "version": "1.0.0", "sha256": "a" * 64, "size": 1,
            "machines": [platform.machine()],
            "python": [f"{sys.version_info.major}.{sys.version_info.minor}"],
            "updater_protocol": 1, "state_schema": 1,
        }
        write_json(self.home / "config.json", {
            "state_dir": str(self.state), "server": "https://example.com", "arguments": [],
        })
        write_json(self.home / "record.json", {
            "current": "bootstrap", "sequence": 0, "version": "bootstrap",
            "highest": 0, "failed": {}, "pending": None,
        })

    def signed(self, **changes):
        manifest = {**self.manifest, **changes}
        path = self.root / "manifest"
        signature = self.root / "signature"
        path.write_bytes(canonical(manifest))
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(self.private), "-rawin",
             "-in", str(path), "-out", str(signature)], check=True, capture_output=True,
        )
        return {"manifest": manifest, "signature": base64.b64encode(signature.read_bytes()).decode()}

    def fake_release(self, name, fails=False):
        release = self.home / "releases" / name
        (release / "venv/bin").mkdir(parents=True)
        (release / "venv/bin/python").symlink_to(sys.executable)
        (release / "player").mkdir()
        (release / "player/__init__.py").touch()
        (release / "player/__main__.py").write_text(
            "raise SystemExit(1)\n" if fails else
            "import json, os, time\n"
            "from pathlib import Path\n"
            f"path = Path({str(self.state / 'startup-health.json')!r})\n"
            "while True:\n"
            "    temporary = path.with_suffix('.tmp')\n"
            "    temporary.write_text(json.dumps({'id': os.environ['PISIGN_HEALTH_ID'], 'time': time.monotonic()}))\n"
            "    temporary.replace(path)\n"
            "    time.sleep(0.05)\n",
        )
        return name

    def test_real_signatures_and_tamper_rejection(self):
        envelope = self.signed()
        self.assertEqual(verify(envelope, self.public), self.manifest)
        envelope["manifest"]["version"] = "9.0.0"
        with self.assertRaisesRegex(ValueError, "signature verification"):
            verify(envelope, self.public)
        for signature in ("bad", None, "A" * 88):
            with self.assertRaises(ValueError):
                verify({**self.signed(), "signature": signature}, self.public)

    def test_bad_signature_never_stages_or_advances_replay_floor(self):
        supervisor = Supervisor(self.home)
        supervisor.stage = Mock()
        envelope = self.signed()
        envelope["manifest"]["sequence"] = 100
        with self.assertRaises(ValueError):
            supervisor.update(envelope)
        supervisor.stage.assert_not_called()
        self.assertEqual(read_json(self.home / "record.json")["highest"], 0)

    def test_incompatible_release_is_reported_and_not_installed(self):
        supervisor = Supervisor(self.home)
        supervisor.stage = Mock()
        supervisor.update(self.signed(state_schema=2))
        supervisor.stage.assert_not_called()
        self.assertEqual(read_json(self.state / "update-status.json")["status"], "incompatible")
        self.assertEqual(supervisor.record["current"], "bootstrap")

    def test_known_failed_and_older_releases_are_not_retried_after_reboot(self):
        supervisor = Supervisor(self.home)
        supervisor.stage = Mock(side_effect=ValueError("broken archive"))
        with self.assertLogs("pisign-updater", level="ERROR"):
            supervisor.update(self.signed())
        supervisor.stage.assert_called_once()
        restarted = Supervisor(self.home)
        restarted.stage = Mock()
        restarted.recover()
        restarted.update(self.signed())
        restarted.stage.assert_not_called()
        report = read_json(self.state / "update-status.json")
        self.assertEqual(report["status"], "failed")
        self.assertIn("broken archive", report["error"])
        self.assertEqual(restarted.record["current"], "bootstrap")

    def test_network_failure_retries_after_reconnect_without_blacklisting_release(self):
        supervisor = Supervisor(self.home)
        supervisor.stage = Mock(side_effect=URLError("offline"))
        with self.assertLogs("pisign-updater", level="WARNING"):
            supervisor.update(self.signed())
        self.assertEqual(read_json(self.state / "update-status.json")["status"], "retrying")
        self.assertNotIn("1", supervisor.record["failed"])
        supervisor.update(self.signed())
        supervisor.stage.assert_called_once()
        restarted = Supervisor(self.home)
        restarted.record["retry_at"] = 0
        restarted.stage = Mock(side_effect=ValueError("reconnected and downloaded bad archive"))
        with self.assertLogs("pisign-updater", level="ERROR"):
            restarted.update(self.signed())
        restarted.stage.assert_called_once()
        self.assertIn("1", restarted.record["failed"])

    def test_older_signed_release_is_not_installed_after_newer_incompatible_release(self):
        supervisor = Supervisor(self.home)
        supervisor.stage = Mock()
        supervisor.update(self.signed(sequence=2, state_schema=2))
        supervisor.update(self.signed())
        supervisor.stage.assert_not_called()
        self.assertEqual(supervisor.record["highest"], 2)

    def test_interrupted_activation_rolls_back_without_retry(self):
        supervisor = Supervisor(self.home)
        supervisor.record.update(highest=1, pending={
            "sequence": 1, "version": "1.0.0", "release": "candidate",
        })
        supervisor.save()
        restarted = Supervisor(self.home)
        restarted.recover()
        self.assertIsNone(restarted.record["pending"])
        self.assertEqual(restarted.record["current"], "bootstrap")
        self.assertIn("1", restarted.record["failed"])
        self.assertEqual(read_json(self.state / "update-status.json")["status"], "rolled_back")

    def test_interrupted_staging_can_retry_without_changing_running_release(self):
        supervisor = Supervisor(self.home)
        supervisor.record["highest"] = 1
        supervisor.status("staging", target="1.0.0")
        restarted = Supervisor(self.home)
        restarted.recover()
        self.assertEqual(restarted.record["current"], "bootstrap")
        self.assertNotIn("1", restarted.record["failed"])
        self.assertEqual(read_json(self.state / "update-status.json")["status"], "retrying")

    def test_startup_success_commits_and_preserves_every_state_surface(self):
        self.fake_release("bootstrap")
        self.fake_release("candidate")
        for name in ("credentials.json", "playlist.json", "browser-profile/Cookies", "assets/cached"):
            path = self.state / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"preserve exactly")
        supervisor = Supervisor(self.home)
        supervisor.stage = Mock(return_value="candidate")
        self.addCleanup(supervisor.stop_child)
        supervisor.launch("bootstrap")
        with patch("player.updater.HEALTH_WINDOW", 0), patch("player.updater.HEALTH_TIMEOUT", 5):
            supervisor.update(self.signed())
        self.assertEqual(supervisor.record["current"], "candidate")
        self.assertEqual(supervisor.record["sequence"], 1)
        self.assertEqual(read_json(self.state / "update-status.json")["status"], "installed")
        for name in ("credentials.json", "playlist.json", "browser-profile/Cookies", "assets/cached"):
            self.assertEqual((self.state / name).read_bytes(), b"preserve exactly")
        supervisor.stage.reset_mock()
        supervisor.update(self.signed())
        supervisor.stage.assert_not_called()

    def test_real_failed_child_is_rolled_back_and_suppressed(self):
        self.fake_release("bootstrap")
        self.fake_release("candidate", fails=True)
        supervisor = Supervisor(self.home)
        supervisor.stage = Mock(return_value="candidate")
        self.addCleanup(supervisor.stop_child)
        supervisor.launch("bootstrap")
        with patch("player.updater.HEALTH_TIMEOUT", 3):
            supervisor.update(self.signed())
        self.assertEqual(supervisor.record["current"], "bootstrap")
        self.assertIsNone(supervisor.record["pending"])
        self.assertIsNone(supervisor.child.poll())
        self.assertEqual(read_json(self.state / "update-status.json")["status"], "rolled_back")
        supervisor.update(self.signed())
        supervisor.stage.assert_called_once()

    def test_missing_or_stale_health_never_confirms_startup(self):
        supervisor = Supervisor(self.home)
        supervisor.child = Mock()
        supervisor.child.poll.return_value = None
        supervisor.health_id = "current"
        for receipt in (None, {"id": "previous", "time": 99999999999}, {"id": "current", "time": 0}):
            write_json(self.state / "startup-health.json", receipt)
            self.assertFalse(supervisor.healthy())

    def test_health_confirmation_requires_thirty_continuous_seconds(self):
        supervisor = Supervisor(self.home)
        supervisor.stage = Mock(return_value="candidate")
        supervisor.child = Mock()
        supervisor.child.poll.return_value = None
        supervisor.launch = Mock()
        supervisor.stop_child = Mock()
        clock = [0]
        supervisor.healthy = Mock(side_effect=lambda: clock[0] != 15)

        def sleep(seconds):
            clock[0] += seconds

        with patch("player.updater.time.monotonic", side_effect=lambda: clock[0]), \
                patch("player.updater.time.sleep", side_effect=sleep):
            supervisor.update(self.signed())
        self.assertEqual(clock[0], 46)
        self.assertEqual(supervisor.record["current"], "candidate")

    def test_no_health_rolls_back_at_exactly_120_seconds(self):
        supervisor = Supervisor(self.home)
        supervisor.stage = Mock(return_value="candidate")
        supervisor.child = Mock()
        supervisor.child.poll.return_value = None
        supervisor.launch = Mock()
        supervisor.stop_child = Mock()
        supervisor.healthy = Mock(return_value=False)
        clock = [0]

        def sleep(seconds):
            clock[0] += seconds

        with patch("player.updater.time.monotonic", side_effect=lambda: clock[0]), \
                patch("player.updater.time.sleep", side_effect=sleep):
            supervisor.update(self.signed())
        self.assertEqual(clock[0], 120)
        self.assertEqual(supervisor.record["current"], "bootstrap")
        supervisor.launch.assert_called_with("bootstrap")

    def test_download_checks_signed_size_and_hash_and_uses_device_channel(self):
        write_json(self.state / "credentials.json", {"server": "https://example.com", "token": "a" * 43})
        supervisor = Supervisor(self.home)
        for content in (b"", b"x", b"xx"):
            archive = self.root / f"download-{len(content)}"
            response = io.BytesIO(content)
            opener = Mock()
            opener.open.return_value = response
            with patch("player.updater.build_opener", return_value=opener), self.assertRaises(ValueError):
                supervisor.download(self.manifest, archive)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://example.com/api/device/releases/1/")
            self.assertEqual(request.get_header("Authorization"), "Bearer " + "a" * 43)

    def test_bootstrap_command_replaces_launcher_with_independent_supervisor(self):
        fresh_home = self.root / "new-updater"
        arguments = [
            "player", "install-updater", "--server", "https://example.com",
            "--state-dir", str(self.state), "--update-dir", str(fresh_home),
            "--public-key", str(self.public), "--software-rendering",
        ]
        with patch.object(sys, "argv", arguments), patch("player.updater.bootstrap") as install, \
                patch("player.__main__.install_autostart") as autostart:
            main()
        self.assertIn("--software-rendering", install.call_args.args[3])
        self.assertEqual(autostart.call_args.args[2], [
            "/usr/bin/python3", str(fresh_home / "updater.py"), "--home", str(fresh_home),
        ])

    def test_live_child_without_healthy_playback_is_rolled_back_at_deadline(self):
        self.fake_release("bootstrap")
        self.fake_release("candidate")
        supervisor = Supervisor(self.home)
        supervisor.stage = Mock(return_value="candidate")
        supervisor.healthy = Mock(return_value=False)
        self.addCleanup(supervisor.stop_child)
        supervisor.launch("bootstrap")
        with patch("player.updater.HEALTH_TIMEOUT", 1):
            supervisor.update(self.signed())
        self.assertEqual(supervisor.record["current"], "bootstrap")
        self.assertEqual(read_json(self.state / "update-status.json")["status"], "rolled_back")

    def test_unsafe_archives_and_unpinned_dependencies_are_rejected(self):
        for name, kind in [
            ("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
            ("player/link", tarfile.SYMTYPE), ("venv/bin/python", tarfile.REGTYPE),
        ]:
            with self.subTest(name=name):
                archive = self.root / "bad.tar.gz"
                with tarfile.open(archive, "w:gz") as output:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = "/etc/passwd"
                    output.addfile(info, io.BytesIO())
                with self.assertRaises(ValueError):
                    extract_release(archive, self.root / "extract")
        archive = self.root / "unpinned.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for name, body in (("player/__main__.py", b""), ("requirements.txt", b"segno>=1\n")):
                info = tarfile.TarInfo(name)
                info.size = len(body)
                output.addfile(info, io.BytesIO(body))
        with self.assertRaisesRegex(ValueError, "exact versions"):
            extract_release(archive, self.root / "unpinned")

    def test_bootstrap_refuses_nested_state_or_overwriting_trust(self):
        with self.assertRaisesRegex(ValueError, "separate"):
            bootstrap(self.state / "updater", self.state, "https://example.com", [], self.public)
        with self.assertRaisesRegex(ValueError, "already installed"):
            bootstrap(self.home, self.state, "https://example.com", [], self.public)

    def test_poll_reports_status_and_delivers_release_without_caching_it_in_playlist(self):
        supervisor = Supervisor(self.home)
        supervisor.status("idle")
        write_json(self.state / "credentials.json", {"server": "https://example.com", "token": "a" * 43})
        worker = ConfigWorker("https://example.com", self.state, "Pi")
        worker.api = Mock()
        envelope = self.signed()
        config = {"state": "paired", "slides": [], "release": envelope}
        worker.api.call.return_value = config
        worker.stopping.wait = Mock(side_effect=lambda _: worker.stopping.set())
        with patch.dict(os.environ, {"PISIGN_UPDATE_HOME": str(self.home)}):
            worker.run()
        self.assertEqual(read_json(self.state / "desired-release.json"), envelope)
        self.assertNotIn("release", read_json(self.state / "playlist.json")["config"])
        worker.api.call.assert_called_once_with(
            "/api/device/config/", token="a" * 43, data={"update": read_json(self.state / "update-status.json")},
        )

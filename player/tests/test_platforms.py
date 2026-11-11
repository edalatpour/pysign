import os
import queue
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from player.__main__ import main
from player.browser import Browser, find_browser
from player.locking import player_lock
from player.runtime import run_player
from player.storage import private_directory, read_json, write_json


class PlatformTests(unittest.TestCase):
    def test_player_lock_excludes_other_processes_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "player.lock"
            script = (
                "import sys; from pathlib import Path; from player.locking import player_lock; "
                "lock=player_lock(Path(sys.argv[1])); lock.__enter__()"
            )
            with player_lock(path):
                result = subprocess.run(
                    [sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("BlockingIOError", result.stderr)
            result = subprocess.run(
                [sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_posix_lock_uses_nonblocking_flock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "player.lock"
            flock = Mock()
            fake_fcntl = types.SimpleNamespace(flock=flock, LOCK_EX=2, LOCK_NB=4)
            with patch.dict(sys.modules, {"fcntl": fake_fcntl}), patch("player.locking.os.name", "posix"):
                with player_lock(path):
                    flock.assert_called_once()
                    self.assertEqual(flock.call_args.args[1], 6)

    def test_windows_restart_runs_again_without_spawning_a_process(self):
        with patch("player.__main__.sys.platform", "win32"), \
                patch("player.__main__.run_once", side_effect=[True, False]) as run, \
                patch("player.__main__.os.execv") as execute:
            main()
        self.assertEqual(run.call_count, 2)
        execute.assert_not_called()

    def test_install_commands_rejected_outside_linux(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in ("install-autostart", "install-updater"):
                with self.subTest(command=command), \
                        patch("player.__main__.sys.platform", "win32"), \
                        patch("player.__main__.sys.argv", [
                            "player", command, "--server", "https://example.com", "--state-dir", directory,
                        ]), self.assertRaisesRegex(SystemExit, "only supported on Linux"):
                    main()

    def test_windows_ignores_raspberry_pi_wifi_failure_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "failed").touch()
            worker, browser = Mock(), Mock()
            worker.updates.get_nowait.side_effect = queue.Empty
            browser.process.poll.return_value = None
            with patch("player.runtime.sys.platform", "win32"), \
                    patch("player.runtime.STATUS_DIRECTORY", root), \
                    patch("player.runtime.time.sleep", side_effect=KeyboardInterrupt), \
                    self.assertRaises(KeyboardInterrupt):
                run_player(browser, worker, root, "https://example.com")
            browser.status.assert_called_once_with(
                "Connecting to PiSign", "Waiting for the server.\nCheck the network connection.",
            )

    @unittest.skipUnless(os.name == "nt", "Windows ACL and browser discovery")
    def test_windows_credential_acl_and_browser_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            state = private_directory(Path(directory) / "state")
            credentials = state / "credentials.json"
            write_json(credentials, {"token": "test"})
            self.assertEqual(read_json(credentials), {"token": "test"})
            result = subprocess.run(["icacls", str(credentials)], capture_output=True, text=True, check=True)
            self.assertNotIn("(I)", result.stdout)
            self.assertEqual(result.stdout.count("(F)"), 2)
            chrome = Path(directory) / "Google" / "Chrome" / "Application" / "chrome.exe"
            chrome.parent.mkdir(parents=True)
            chrome.touch()
            with patch.dict(os.environ, {"PROGRAMFILES": directory}, clear=True):
                self.assertEqual(find_browser(), str(chrome))
            self.assertEqual(Browser(state, executable="custom.exe").executable, "custom.exe")

    @unittest.skipUnless(os.name == "nt", "Windows ACL failure handling")
    def test_windows_acl_failure_does_not_write_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            state = private_directory(Path(directory) / "state")
            with patch("player.storage.restrict_windows_access", side_effect=PermissionError("ACL refused")):
                with self.assertRaisesRegex(PermissionError, "ACL refused"):
                    write_json(state / "credentials.json", {"token": "test"})
            self.assertEqual(list(state.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

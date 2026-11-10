import contextlib
import io
import json
import os
import queue
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from player import wifi
from player.runtime import run_player
from player.storage import write_json


class WifiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.boot = self.root / "boot"
        self.boot.mkdir()
        self.connections = self.root / "connections"
        self.connections.mkdir()
        self.status = self.root / "run"
        self.source = self.boot / "pisign-wifi.json"
        self.config = {"ssid": "My network", "password": "test password", "country": "GB"}
        self.old = str(uuid.uuid4())
        self.run = self.enterContext(patch("player.wifi.subprocess.run"))
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout=self.old, stderr="")

    def write_config(self, **changes):
        self.source.write_text(json.dumps({**self.config, **changes}), encoding="utf-8")

    def provision(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return wifi.provision(self.boot, self.status, self.connections)

    def commands(self):
        return [call.args[0] for call in self.run.call_args_list]

    def test_absent_file_does_not_change_network_or_create_status(self):
        self.assertEqual(self.provision(), 0)
        self.run.assert_not_called()
        self.assertFalse(self.status.exists())

    def test_valid_config_and_utf8_bom(self):
        self.source.write_text(json.dumps(self.config), encoding="utf-8-sig")
        self.assertEqual(wifi.read_configuration(self.source), {**self.config, "hidden": False})

    def test_invalid_configuration_is_retained_and_reported_without_network_changes(self):
        invalid = [
            [], None, {},
            {**self.config, "ssid": ""},
            {**self.config, "ssid": 1},
            {**self.config, "ssid": "\0"},
            {**self.config, "ssid": "\ud800"},
            {**self.config, "ssid": "a" * 33},
            {**self.config, "ssid": "\u00e9" * 17},
            {**self.config, "password": "short"},
            {**self.config, "password": "x" * 64},
            {**self.config, "password": "newline\npassword"},
            {**self.config, "password": "\u00e9" * 8},
            {**self.config, "password": None},
            {**self.config, "country": "gb"},
            {**self.config, "country": "GB\n[connection]"},
            {**self.config, "country": None},
            {**self.config, "hidden": "true"},
            {**self.config, "hidden": 1},
            {**self.config, "command": "ignored?"},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.source.write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual(self.provision(), 1)
                self.assertTrue(self.source.exists())
                self.run.assert_not_called()
                self.assertTrue((self.status / "failed").exists())
                self.assertIn("ERROR:", (self.boot / "pisign-wifi-status.txt").read_text())

    def test_invalid_json_and_duplicate_fields(self):
        for raw in [b"{", b"\xff", b"[]" * 3000, b'{"ssid":"one","ssid":"two"}', b"[" * 1100]:
            with self.subTest(raw=raw[:20]):
                self.source.write_bytes(raw)
                self.assertEqual(self.provision(), 1)
                self.run.assert_not_called()

    def test_symlinks_directories_and_fifos_are_not_imported(self):
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps(self.config))
        self.source.symlink_to(target)
        self.assertEqual(self.provision(), 1)
        self.assertEqual(json.loads(target.read_text()), self.config)
        self.source.unlink()
        self.source.symlink_to(self.root / "missing")
        self.assertEqual(self.provision(), 1)
        self.source.unlink()
        self.source.mkdir()
        self.assertEqual(self.provision(), 1)
        self.source.rmdir()
        os.mkfifo(self.source)
        self.assertEqual(self.provision(), 1)
        self.run.assert_not_called()

    def test_ssid_boundaries_hidden_and_raw_psk(self):
        for ssid in ["a", "a" * 32, "\u00e9" * 16]:
            with self.subTest(ssid=ssid):
                self.write_config(ssid=ssid, hidden=True, password="a" * 64)
                config = wifi.read_configuration(self.source)
                self.assertEqual(config["ssid"], ssid)
                self.assertTrue(config["hidden"])
                self.assertEqual(config["password"], "a" * 64)

    def test_keyfile_escapes_password_and_encodes_ssid_as_bytes(self):
        config = {**self.config, "ssid": "x\n[connection]\n", "password": " a\\b;=$() ", "hidden": True}
        content = wifi.keyfile(config, self.old)
        self.assertEqual(content.count("[connection]"), 1)
        self.assertIn("ssid=120;10;91;99;111;110;110;101;99;116;105;111;110;93;10;", content)
        self.assertIn("psk=\\sa\\\\b;=$()\\s\n", content)
        self.assertIn("hidden=true", content)
        self.assertIn("autoconnect=false", content)

    def test_keyfile_with_real_networkmanager_parser_when_available(self):
        try:
            import gi

            gi.require_version("NM", "1.0")
            from gi.repository import GLib, NM
        except (ImportError, ValueError):
            self.skipTest("Optional system NetworkManager introspection library is not installed.")
        cases = [
            ("My Wi-Fi", "test password"),
            ("x\n[connection]\n", " a\\b;=$() "),
            ("\u00e9" * 16, "a" * 64),
            ("123", '"quoted"pass'),
        ]
        for ssid, password in cases:
            with self.subTest(ssid=ssid):
                content = wifi.keyfile(
                    {"ssid": ssid, "password": password, "hidden": True}, self.old,
                )
                parsed = GLib.KeyFile.new()
                parsed.load_from_data(content, len(content.encode()), GLib.KeyFileFlags.NONE)
                connection = NM.keyfile_read(parsed, "/", NM.KeyfileHandlerFlags.NONE)
                self.assertTrue(connection.verify())
                self.assertEqual(bytes(connection.get_setting_wireless().get_ssid().get_data()), ssid.encode())
                self.assertEqual(connection.get_setting_wireless_security().get_psk(), password)
                self.assertTrue(connection.get_setting_wireless().get_hidden())
                self.assertFalse(connection.get_setting_connection().get_autoconnect())

    def test_success_uses_private_profile_no_secrets_in_commands_and_consumes_only_after_activation(self):
        self.write_config(hidden=True)
        observed = []

        def execute(arguments, **kwargs):
            self.assertTrue(self.source.exists())
            if "load" in arguments:
                profile = Path(arguments[-1])
                observed.append(profile)
                self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
                self.assertIn("hidden=true", profile.read_text())
            self.assertNotIn(self.config["password"], " ".join(arguments))
            self.assertNotIn(self.config["ssid"], " ".join(arguments))
            self.assertEqual(kwargs["env"]["LC_ALL"], "C")
            return subprocess.CompletedProcess(arguments, 0, stdout=self.old, stderr="")

        self.run.side_effect = execute
        self.assertEqual(self.provision(), 0)
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0].exists())
        self.assertFalse(self.source.exists())
        self.assertFalse((self.status / "failed").exists())
        self.assertEqual(self.status.stat().st_mode & 0o777, 0o755)
        self.assertIn("SUCCESS:", (self.boot / "pisign-wifi-status.txt").read_text())
        self.assertEqual(self.commands()[1], [wifi.RASPI_CONFIG, "nonint", "do_wifi_country", "GB"])
        activation = next(args for args in self.commands() if "up" in args)
        saved = next(args for args in self.commands() if "modify" in args)
        self.assertEqual(activation[activation.index("uuid") + 1], saved[saved.index("uuid") + 1])
        self.assertEqual(saved[-4:], ["connection.autoconnect", "yes", "connection.autoconnect-priority", "10"])
        self.assertFalse(any("delete" in args for args in self.commands()))

    def test_wrong_password_removes_only_candidate_and_restores_previous_connection(self):
        self.write_config()
        existing = self.connections / "existing.nmconnection"
        existing.write_text("unchanged")
        activations = 0

        def execute(arguments, **kwargs):
            nonlocal activations
            if "up" in arguments:
                activations += 1
                if activations == 1:
                    return subprocess.CompletedProcess(arguments, 4, stdout="", stderr=self.config["password"])
            return subprocess.CompletedProcess(arguments, 0, stdout=self.old, stderr="")

        self.run.side_effect = execute
        self.assertEqual(self.provision(), 1)
        self.assertTrue(self.source.exists())
        self.assertEqual(list(self.connections.iterdir()), [existing])
        self.assertEqual(existing.read_text(), "unchanged")
        deleted = next(args for args in self.commands() if "delete" in args)
        self.assertNotIn(self.old, deleted)
        self.assertEqual(self.commands()[-1][-4:], ["uuid", self.old, "ifname", "wlan0"])
        status = (self.boot / "pisign-wifi-status.txt").read_text()
        self.assertIn("Connecting to Wi-Fi", status)
        self.assertNotIn(self.config["password"], status)

    def test_first_boot_failure_does_not_attempt_to_restore_nonexistent_connection(self):
        for empty in ["", "--"]:
            with self.subTest(active=empty):
                self.write_config()
                self.run.reset_mock()

                def execute(arguments, **kwargs):
                    return subprocess.CompletedProcess(
                        arguments, 4 if "up" in arguments else 0, stdout=empty, stderr="",
                    )

                self.run.side_effect = execute
                self.assertEqual(self.provision(), 1)
                self.assertEqual(len([args for args in self.commands() if "up" in args]), 1)
                self.assertEqual(list(self.connections.iterdir()), [])

    def test_invalid_active_uuid_does_not_change_profiles(self):
        self.write_config()
        self.run.return_value.stdout = "not-a-uuid"
        self.assertEqual(self.provision(), 1)
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(list(self.connections.iterdir()), [])

    def test_country_and_profile_load_failures_do_not_delete_previous_profiles(self):
        for failed_action in ["do_wifi_country", "load", "modify"]:
            with self.subTest(action=failed_action):
                self.write_config()
                self.run.reset_mock()

                def execute(arguments, **kwargs):
                    return subprocess.CompletedProcess(
                        arguments, 1 if failed_action in arguments else 0, stdout=self.old, stderr="",
                    )

                self.run.side_effect = execute
                self.assertEqual(self.provision(), 1)
                self.assertTrue(self.source.exists())
                self.assertEqual(list(self.connections.iterdir()), [])
                for args in self.commands():
                    if "delete" in args:
                        self.assertNotIn(self.old, args)

    def test_cleanup_failure_is_explicit(self):
        self.write_config()

        def execute(arguments, **kwargs):
            return subprocess.CompletedProcess(
                arguments, 1 if "up" in arguments or "delete" in arguments else 0,
                stdout=self.old, stderr="",
            )

        self.run.side_effect = execute
        self.assertEqual(self.provision(), 1)
        self.assertIn("cleanup or reconnection also failed", (self.boot / "pisign-wifi-status.txt").read_text())
        self.assertTrue(self.source.exists())

    def test_failed_import_can_be_corrected_and_success_clears_failure_marker(self):
        self.write_config(password="bad")
        self.assertEqual(self.provision(), 1)
        self.write_config()
        self.assertEqual(self.provision(), 0)
        self.assertFalse((self.status / "failed").exists())
        self.assertFalse(self.source.exists())

    def test_unlink_failure_rolls_back_and_retains_import(self):
        self.write_config()
        original_unlink = Path.unlink

        def unlink(path, **kwargs):
            if path == self.source:
                raise PermissionError
            return original_unlink(path, **kwargs)

        with patch.object(Path, "unlink", autospec=True, side_effect=unlink):
            self.assertEqual(self.provision(), 1)
        self.assertTrue(self.source.exists())
        self.assertEqual(list(self.connections.iterdir()), [])
        self.assertTrue(any("delete" in args for args in self.commands()))
        self.assertIn(self.old, self.commands()[-1])

    def test_command_timeout_and_missing_tool_do_not_expose_exception_details(self):
        for error in [
            subprocess.TimeoutExpired(["secret"], 1, output="secret"),
            FileNotFoundError("secret"),
        ]:
            with self.subTest(error=type(error).__name__):
                self.run.side_effect = error
                with self.assertRaises(wifi.WifiError) as caught:
                    wifi.command(["tool"], "Wi-Fi operation")
                self.assertNotIn("secret", str(caught.exception))

    def test_activation_timeout_rolls_back_and_keeps_configuration(self):
        self.write_config()

        def execute(arguments, **kwargs):
            if "up" in arguments and self.old not in arguments:
                raise subprocess.TimeoutExpired(arguments, 70, output=self.config["password"])
            return subprocess.CompletedProcess(arguments, 0, stdout=self.old, stderr="")

        self.run.side_effect = execute
        self.assertEqual(self.provision(), 1)
        self.assertEqual(list(self.connections.iterdir()), [])
        self.assertTrue(self.source.exists())
        self.assertIn(self.old, self.commands()[-1])
        self.assertNotIn(self.config["password"], (self.boot / "pisign-wifi-status.txt").read_text())

    @patch("player.wifi.os.umask")
    def test_main_requires_root_and_reports_status_write_failures(self, umask):
        with patch("player.wifi.os.geteuid", return_value=1000), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(wifi.main(), 1)
        self.assertIn("root-owned", output.getvalue())
        with patch("player.wifi.os.geteuid", return_value=0), \
                patch("player.wifi.provision", side_effect=OSError("sensitive path")), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(wifi.main(), 1)
        self.assertNotIn("sensitive path", output.getvalue())

    def test_runtime_shows_setup_error_when_unconfigured_but_keeps_cached_playback(self):
        self.status.mkdir()
        (self.status / "failed").touch()
        state = self.root / "state"
        state.mkdir()
        for cached in [False, True]:
            with self.subTest(cached=cached):
                if cached:
                    write_json(state / "playlist.json", {
                        "server": "https://example.com",
                        "config": {"state": "paired", "name": "Pi", "slides": [
                            {"title": "Page", "url": "https://example.com", "duration": 30},
                        ]},
                    })
                worker, browser = Mock(), Mock()
                worker.updates.get_nowait.side_effect = queue.Empty
                browser.process.poll.return_value = None
                with patch("player.runtime.STATUS_DIRECTORY", self.status), \
                        patch("player.runtime.time.sleep", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        run_player(browser, worker, state, "https://example.com")
                if cached:
                    browser.navigate.assert_called_once()
                    browser.status.assert_not_called()
                else:
                    browser.status.assert_called_once_with("Wi-Fi setup failed", wifi.FAILURE_MESSAGE)
                worker.stopping.set.assert_called_once()
                browser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

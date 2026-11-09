import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from player.__main__ import install_autostart, main, parser
from player.browser import Browser, BrowserError
from player.client import (
    API, APIError, ConfigWorker, CredentialRevoked, NoRedirects, ResetRequested, RestartRequested,
    server_url, validate_config, validate_restart,
)
from player.runtime import Rotation, cached_config, run_player
from player.storage import private_directory, read_json, reset_state, write_json


def playlist():
    return {"state": "paired", "name": "Lobby display", "slides": [
        {"title": "Scoreboard", "url": "https://example.com/scores", "duration": 5, "show_qr": True, "qr_url": ""},
        {"title": "Calendar", "url": "https://example.com/calendar", "duration": 5, "show_qr": True, "qr_url": ""},
    ]}


class RotationTests(unittest.TestCase):
    def test_exact_five_second_cycle_and_repeat(self):
        rotation = Rotation()
        slides = playlist()["slides"]
        rotation.configure(slides)
        self.assertEqual(rotation.due(0), slides[0])
        self.assertIsNone(rotation.due(4.999))
        self.assertEqual(rotation.due(5), slides[1])
        self.assertIsNone(rotation.due(9.999))
        self.assertEqual(rotation.due(10), slides[0])

    def test_variable_durations_and_playlist_updates(self):
        rotation = Rotation()
        slides = playlist()["slides"]
        slides[1]["duration"] = 17
        rotation.configure(slides)
        rotation.due(0)
        rotation.due(5)
        self.assertIsNone(rotation.due(21.999))
        self.assertEqual(rotation.due(22), slides[0])
        rotation.configure([slides[1]])
        self.assertEqual(rotation.due(23), slides[1])
        rotation.configure([])
        self.assertIsNone(rotation.due(100))

    def test_identical_poll_does_not_restart_playlist(self):
        rotation = Rotation()
        slides = playlist()["slides"]
        rotation.configure(slides)
        rotation.due(0)
        rotation.configure(playlist()["slides"])
        self.assertIsNone(rotation.due(4))
        self.assertEqual(rotation.due(5), slides[1])

    def test_qr_only_change_restarts_rotation_with_new_setting(self):
        rotation = Rotation()
        slides = playlist()["slides"]
        rotation.configure(slides)
        rotation.due(0)
        changed = [{**slides[0], "show_qr": False}, slides[1]]
        rotation.configure(changed)
        self.assertEqual(rotation.due(1), changed[0])


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

    def test_config_validation_rejects_invalid_device_names(self):
        for name in [None, 123, "", " \n", "a" * 101]:
            with self.subTest(name=name):
                config = playlist()
                config["name"] = name
                with self.assertRaisesRegex(ValueError, "Device name"):
                    validate_config(config)

    def test_qr_setting_requires_a_boolean(self):
        for value in [None, 0, 1, "", "false", [], {}]:
            with self.subTest(value=value):
                config = playlist()
                config["slides"][0]["show_qr"] = value
                with self.assertRaisesRegex(ValueError, "show_qr must be a boolean"):
                    validate_config(config)

    def test_legacy_qr_setting_defaults_to_true(self):
        legacy = playlist()
        for slide in legacy["slides"]:
            del slide["show_qr"]
            del slide["qr_url"]
        self.assertEqual(validate_config(legacy), playlist())
        write_json(self.state / "playlist.json", {"server": self.server, "config": legacy})
        self.assertEqual(cached_config(self.state, self.server), playlist())

    def test_disabled_qr_setting_survives_polling_and_offline_cache(self):
        config = playlist()
        config["slides"][0]["show_qr"] = False
        config["slides"][0]["qr_url"] = "https://example.org/scan"
        worker = ConfigWorker(self.server, self.state, "Pi")
        write_json(self.state / "credentials.json", {"server": self.server, "token": "a" * 43})
        worker.api = Mock()
        worker.api.call.return_value = config
        self.assertEqual(self.poll_once(worker), ("config", config))
        self.assertEqual(cached_config(self.state, self.server), config)

    def test_custom_qr_url_validation(self):
        for value in [
            None, 1, False, [], {}, "not-a-url", "javascript:alert(1)",
            "file:///tmp", "https://user:password@example.com/",
            "https://example.com/" + "a" * 2048,
        ]:
            with self.subTest(value=value):
                config = playlist()
                config["slides"][0]["qr_url"] = value
                with self.assertRaises(ValueError):
                    validate_config(config)
        config = playlist()
        config["slides"][0]["qr_url"] = "https://example.org/scan?source=display#info"
        self.assertEqual(validate_config(config), config)

    def test_legacy_cached_config_without_name_is_still_valid(self):
        config = playlist()
        del config["name"]
        write_json(self.state / "playlist.json", {"server": self.server, "config": config})
        self.assertEqual(cached_config(self.state, self.server), config)

    def test_unpaired_config_does_not_require_a_name(self):
        config = {"state": "unpaired", "slides": []}
        self.assertEqual(validate_config(config), config)

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

    def restart_command(self):
        return {
            "id": str(uuid.uuid4()),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        }

    def poll_once(self, worker):
        worker.stopping.clear()
        worker.stopping.wait = Mock(side_effect=lambda _: worker.stopping.set())
        worker.run()
        return worker.updates.get_nowait()

    def test_restart_is_delivered_but_not_cached(self):
        command = self.restart_command()
        write_json(self.state / "credentials.json", {"server": self.server, "token": "a" * 43})
        worker = ConfigWorker(self.server, self.state, "Pi")
        worker.api = Mock()
        worker.api.call.return_value = {**playlist(), "restart": command}
        self.assertEqual(self.poll_once(worker), ("config", {**playlist(), "restart": command}))
        self.assertEqual(cached_config(self.state, self.server), playlist())
        self.assertFalse((self.state / "restart.json").exists())
        worker.api.call.assert_called_once_with("/api/device/config/", token="a" * 43)

    def test_invalid_restart_is_reported_as_fatal(self):
        write_json(self.state / "credentials.json", {"server": self.server, "token": "a" * 43})
        for command in [
            None, {}, {"id": "not-a-uuid", "expires_at": "bad"},
            {"id": str(uuid.uuid4()), "expires_at": "2026-01-01T00:00:00"},
            {"id": str(uuid.uuid4()), "expires_at": 123},
        ]:
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    validate_restart(command)
                worker = ConfigWorker(self.server, self.state, "Pi")
                worker.api = Mock()
                worker.api.call.return_value = {**playlist(), "restart": command}
                self.assertEqual(self.poll_once(worker)[0], "fatal")
                self.assertFalse((self.state / "restart.json").exists())

    def test_expired_restart_is_not_executed(self):
        command = self.restart_command()
        command["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        worker = ConfigWorker(self.server, self.state, "Pi")
        write_json(self.state / "credentials.json", {"server": self.server, "token": "a" * 43})
        worker.api = Mock()
        worker.api.call.return_value = {**playlist(), "restart": command}
        self.assertEqual(self.poll_once(worker), ("config", playlist()))

    def test_receipt_deduplicates_restart_and_waits_for_browser_before_acknowledging(self):
        command = self.restart_command()
        write_json(self.state / "credentials.json", {"server": self.server, "token": "a" * 43})
        write_json(self.state / "restart.json", {
            "server": self.server, "id": command["id"], "acknowledged": False,
        })
        worker = ConfigWorker(self.server, self.state, "Pi")
        worker.api = Mock()
        worker.api.call.return_value = {**playlist(), "restart": command}
        self.assertEqual(self.poll_once(worker), ("config", playlist()))
        worker.api.call.assert_called_once_with("/api/device/config/", token="a" * 43)
        worker.ready.set()
        worker.api.call.side_effect = [
            {**playlist(), "restart": command}, {"status": "completed"},
        ]
        self.assertEqual(self.poll_once(worker), ("config", playlist()))
        self.assertEqual(worker.api.call.call_args_list[-1].args, (
            f"/api/device/restarts/{command['id']}/completed/",
        ))
        self.assertTrue(read_json(self.state / "restart.json")["acknowledged"])
        restarted = ConfigWorker(self.server, self.state, "Pi")
        restarted.api = Mock()
        restarted.api.call.return_value = {**playlist(), "restart": command}
        restarted.ready.set()
        self.assertEqual(self.poll_once(restarted), ("config", playlist()))
        self.assertEqual(restarted.api.call.call_count, 1)

    def test_acknowledgement_outage_retries_without_restart_loop_or_stalling_playlist(self):
        command = self.restart_command()
        write_json(self.state / "credentials.json", {"server": self.server, "token": "a" * 43})
        write_json(self.state / "restart.json", {
            "server": self.server, "id": command["id"], "acknowledged": False,
        })
        worker = ConfigWorker(self.server, self.state, "Pi")
        worker.ready.set()
        worker.api = Mock()
        worker.api.call.side_effect = [
            {**playlist(), "restart": command}, APIError("Offline"),
            {**playlist(), "restart": command}, {"status": "expired"},
        ]
        self.assertEqual(self.poll_once(worker), ("config", playlist()))
        self.assertFalse(read_json(self.state / "restart.json")["acknowledged"])
        self.assertEqual(self.poll_once(worker), ("config", playlist()))
        self.assertTrue(read_json(self.state / "restart.json")["acknowledged"])

    def test_restart_receipt_is_server_bound_and_validated(self):
        for receipt in [
            [], {"server": "https://other.example.com", "id": str(uuid.uuid4()), "acknowledged": False},
            {"server": self.server, "id": "bad", "acknowledged": False},
            {"server": self.server, "id": str(uuid.uuid4()), "acknowledged": "false"},
        ]:
            with self.subTest(receipt=receipt):
                write_json(self.state / "restart.json", receipt)
                with self.assertRaises(ValueError):
                    ConfigWorker(self.server, self.state, "Pi")

    def test_runtime_persists_receipt_and_closes_worker_browser_before_restart(self):
        command = self.restart_command()
        worker = Mock()
        worker.is_alive.return_value = False
        worker.updates.get_nowait.return_value = ("config", {**playlist(), "restart": command})
        browser = Mock()
        browser.close.side_effect = lambda: self.assertFalse((self.state / "restart.json").exists())
        with self.assertRaises(RestartRequested):
            run_player(browser, worker, self.state, self.server)
        self.assertEqual(read_json(self.state / "restart.json"), {
            "server": self.server, "id": command["id"], "acknowledged": False,
        })
        worker.ready.set.assert_not_called()
        worker.stopping.set.assert_called_once()
        worker.join.assert_called_once_with(timeout=22)
        browser.close.assert_called_once()
        browser.navigate.assert_not_called()

    def test_runtime_reports_readiness_only_after_browser_startup(self):
        worker = Mock()
        worker.updates.get_nowait.return_value = ("config", playlist())
        browser = Mock()
        browser.process.poll.return_value = None
        with patch("player.runtime.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_player(browser, worker, self.state, self.server)
        browser.navigate.assert_called_once()
        worker.ready.set.assert_called_once()

    def test_browser_startup_failure_does_not_report_readiness(self):
        worker = Mock()
        worker.updates.get_nowait.return_value = ("config", playlist())
        browser = Mock()
        browser.navigate.side_effect = BrowserError("Chromium failed")
        with patch("player.runtime.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_player(browser, worker, self.state, self.server)
        worker.ready.set.assert_not_called()

    def test_failed_receipt_write_prevents_restart_and_stops_worker_browser(self):
        worker = Mock()
        worker.is_alive.return_value = False
        worker.updates.get_nowait.return_value = ("config", {
            **playlist(), "restart": self.restart_command(),
        })
        browser = Mock()
        with patch("player.runtime.write_json", side_effect=OSError("Disk full")):
            with self.assertRaisesRegex(OSError, "Disk full"):
                run_player(browser, worker, self.state, self.server)
        worker.stopping.set.assert_called_once()
        browser.close.assert_called_once()
        self.assertFalse((self.state / "restart.json").exists())

    def test_worker_shutdown_timeout_aborts_restart_without_receipt(self):
        worker = Mock()
        worker.is_alive.return_value = True
        worker.updates.get_nowait.return_value = ("config", {
            **playlist(), "restart": self.restart_command(),
        })
        browser = Mock()
        with self.assertRaisesRegex(OSError, "Polling worker did not stop"):
            run_player(browser, worker, self.state, self.server)
        worker.stopping.set.assert_called_once()
        browser.close.assert_called_once()
        self.assertFalse((self.state / "restart.json").exists())

    def test_main_reexec_preserves_interpreter_options_and_releases_lock(self):
        arguments = [
            "run", "--server", self.server, "--state-dir", str(self.state),
            "--software-rendering", "--browser", "/opt/chromium", "--name", "Lobby",
        ]
        original = ["/opt/venv/bin/python", "-u", "-m", "player", *arguments]

        def reexec(executable, argv):
            import fcntl
            with (self.state / "player.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(executable, original[0])
            self.assertEqual(argv, original)

        with patch("player.__main__.sys.argv", ["player", *arguments]), \
                patch("player.__main__.sys.orig_argv", original), \
                patch("player.__main__.sys.executable", original[0]), \
                patch("player.__main__.Browser"), \
                patch("player.__main__.run_player", side_effect=RestartRequested), \
                patch("player.__main__.os.execv", side_effect=reexec) as execute:
            main()
        execute.assert_called_once()

    def test_reset_removes_restart_receipt(self):
        write_json(self.state / "restart.json", {
            "server": self.server, "id": str(uuid.uuid4()), "acknowledged": True,
        })
        with patch("player.__main__.sys.argv", ["player", "reset", "--yes", "--state-dir", str(self.state)]):
            main()
        self.assertFalse((self.state / "restart.json").exists())

    def save_reset_receipt(self, command, acknowledged=False):
        write_json(self.state / "reset.json", {
            "server": self.server, "id": command["id"], "acknowledged": acknowledged,
        })
        write_json(self.state / "credentials.json", {"server": self.server, "token": "a" * 43})

    def test_reset_delivery_takes_priority_and_is_never_cached(self):
        command = self.restart_command()
        worker = ConfigWorker(self.server, self.state, "Pi")
        worker.api = Mock()
        write_json(self.state / "credentials.json", {"server": self.server, "token": "a" * 43})
        worker.api.call.return_value = {**playlist(), "reset": command, "restart": self.restart_command()}
        self.assertEqual(self.poll_once(worker), ("config", {**playlist(), "reset": command}))
        self.assertFalse((self.state / "playlist.json").exists())
        self.assertFalse((self.state / "reset.json").exists())

    def test_reset_invalid_expired_and_duplicate_deliveries(self):
        command = self.restart_command()
        self.save_reset_receipt(command, acknowledged=True)
        worker = ConfigWorker(self.server, self.state, "Pi")
        worker.api = Mock()
        worker.api.call.return_value = {**playlist(), "reset": command}
        self.assertEqual(self.poll_once(worker), ("config", playlist()))
        command = self.restart_command()
        command["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        worker.api.call.return_value = {**playlist(), "reset": command}
        self.assertEqual(self.poll_once(worker), ("config", playlist()))
        for invalid in [None, {}, {"id": "bad", "expires_at": "bad"}]:
            worker.api.call.return_value = {**playlist(), "reset": invalid}
            self.assertEqual(self.poll_once(worker)[0], "fatal")

    def test_reset_acknowledgement_retries_before_fetching_or_caching_configuration(self):
        command = self.restart_command()
        self.save_reset_receipt(command)
        worker = ConfigWorker(self.server, self.state, "Pi")
        worker.api = Mock()
        worker.api.call.side_effect = [APIError("Offline")]
        self.assertEqual(self.poll_once(worker)[0], "offline")
        worker.api.call.assert_called_once_with(
            f"/api/device/resets/{command['id']}/completed/", token="a" * 43, data={},
        )
        self.assertFalse((self.state / "playlist.json").exists())
        self.assertFalse(read_json(self.state / "reset.json")["acknowledged"])
        worker.api.call.side_effect = [
            {"status": "completed"}, {"state": "unpaired", "slides": []}, {"code": "ABCDE23456"},
        ]
        self.assertEqual(self.poll_once(worker), ("config", {
            "state": "unpaired", "slides": [], "code": "ABCDE23456",
        }))
        self.assertTrue(read_json(self.state / "reset.json")["acknowledged"])
        worker.api.call.side_effect = [{"state": "unpaired", "slides": []}, {"code": "ABCDE23456"}]
        self.assertEqual(self.poll_once(worker)[0], "config")

    def test_reset_receipt_is_validated_and_local_reset_clears_it(self):
        write_json(self.state / "reset.json", {
            "server": "https://other.example.com", "id": str(uuid.uuid4()), "acknowledged": False,
        })
        with self.assertRaisesRegex(ValueError, "saved reset receipt"):
            ConfigWorker(self.server, self.state, "Pi")
        reset_state(self.state, clear_identity=True)
        self.assertFalse((self.state / "reset.json").exists())

    def test_runtime_saves_reset_receipt_after_shutdown_without_playback(self):
        command = self.restart_command()
        worker = Mock()
        worker.is_alive.return_value = False
        worker.updates.get_nowait.return_value = ("config", {
            **playlist(), "reset": command, "restart": self.restart_command(),
        })
        browser = Mock()
        with self.assertRaises(ResetRequested):
            run_player(browser, worker, self.state, self.server)
        self.assertEqual(read_json(self.state / "reset.json"), {
            "server": self.server, "id": command["id"], "acknowledged": False,
        })
        worker.join.assert_called_once_with(timeout=22)
        browser.close.assert_called_once()
        browser.navigate.assert_not_called()
        self.assertFalse((self.state / "restart.json").exists())

    def test_reset_shutdown_and_receipt_failures_abort_explicitly(self):
        for alive in [True, False]:
            worker = Mock()
            worker.is_alive.return_value = alive
            worker.updates.get_nowait.return_value = ("config", {
                **playlist(), "reset": self.restart_command(),
            })
            with patch("player.runtime.write_json", side_effect=OSError("Disk full")):
                with self.assertRaises(OSError):
                    run_player(Mock(), worker, self.state, self.server)
            self.assertFalse((self.state / "reset.json").exists())

    def test_pending_reset_cleanup_resumes_before_browser_and_preserves_identity(self):
        command = self.restart_command()
        self.save_reset_receipt(command)
        credentials = read_json(self.state / "credentials.json")
        write_json(self.state / "playlist.json", {"server": self.server, "config": playlist()})
        write_json(self.state / "restart.json", {
            "server": self.server, "id": str(uuid.uuid4()), "acknowledged": True,
        })
        profile = private_directory(self.state / "browser-profile")
        (profile / "Cookies").write_bytes(b"cookies")
        (self.state / "status.html").write_text("Old display")

        def browser_started(*args, **kwargs):
            for name in ["playlist.json", "restart.json", "status.html", "browser-profile"]:
                self.assertFalse((self.state / name).exists())
            self.assertEqual(read_json(self.state / "credentials.json"), credentials)
            return Mock()

        with patch("player.__main__.sys.argv", [
            "player", "run", "--server", self.server, "--state-dir", str(self.state),
        ]), patch("player.__main__.Browser", side_effect=browser_started), \
                patch("player.__main__.run_player"):
            main()
        self.assertFalse(read_json(self.state / "reset.json")["acknowledged"])

    def test_reset_cleanup_failure_does_not_reexec_or_acknowledge(self):
        self.save_reset_receipt(self.restart_command())
        with patch("player.__main__.sys.argv", [
            "player", "run", "--server", self.server, "--state-dir", str(self.state),
        ]), patch("player.__main__.reset_state", side_effect=OSError("Permission denied")), \
                patch("player.__main__.Browser") as browser, \
                patch("player.__main__.os.execv") as execute:
            with self.assertRaisesRegex(SystemExit, "Permission denied"):
                main()
        browser.assert_not_called()
        execute.assert_not_called()
        self.assertFalse(read_json(self.state / "reset.json")["acknowledged"])

    def test_unpaired_poll_removes_previous_cache(self):
        write_json(self.state / "playlist.json", {"server": self.server, "config": playlist()})
        write_json(self.state / "credentials.json", {"server": self.server, "token": "a" * 43})
        worker = ConfigWorker(self.server, self.state, "Pi")
        worker.api = Mock()
        worker.api.call.side_effect = [{"state": "unpaired", "slides": []}, {"code": "ABCDE23456"}]
        self.assertEqual(self.poll_once(worker)[0], "config")
        self.assertFalse((self.state / "playlist.json").exists())

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
        self.assertFalse(args.software_rendering)
        self.assertIn("PiSign Raspberry Pi Chromium player", parser().format_help())

    def test_run_passes_software_rendering_to_browser(self):
        arguments = ["pisign-player", "run", "--server", self.server,
                     "--state-dir", str(self.state), "--software-rendering"]
        with patch("player.__main__.sys.argv", arguments):
            with patch("player.__main__.Browser") as browser:
                with patch("player.__main__.ConfigWorker") as worker:
                    with patch("player.__main__.run_player") as run:
                        main()
        browser.assert_called_once_with(self.state, None, software_rendering=True)
        run.assert_called_once_with(browser.return_value, worker.return_value, self.state, self.server)

    def test_autostart_preserves_software_rendering_and_custom_browser(self):
        home = Path(self.temporary.name)
        args = parser().parse_args([
            "install-autostart", "--server", self.server, "--state-dir", str(self.state),
            "--software-rendering", "--browser", "/opt/custom/chromium",
        ])
        with patch("player.__main__.Path.home", return_value=home):
            with patch("player.__main__.shutil.which", return_value="/usr/bin/labwc"):
                install_autostart(args, self.server)
        desktop = (home / ".config/autostart/pisign.desktop").read_text()
        launcher = (self.state / "start-player").read_text()
        self.assertIn('"--software-rendering"', desktop)
        self.assertIn('"/opt/custom/chromium"', desktop)
        self.assertIn("--software-rendering", launcher)
        self.assertIn("/opt/custom/chromium", launcher)
        self.assertIn("start-player", (home / ".config/labwc/autostart").read_text())

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

    def test_ready_screen_shows_cached_name_and_updates_on_poll(self):
        initial = {"state": "paired", "name": "Lobby display", "slides": []}
        changed = {**initial, "name": "Reception <East> & West"}
        write_json(self.state / "playlist.json", {"server": self.server, "config": initial})
        worker = Mock()
        worker.updates.get_nowait.side_effect = [
            ("offline", "Offline"), ("config", validate_config(changed)),
        ]
        browser = Browser(self.state, executable="/usr/bin/chromium")
        with patch.object(browser, "navigate") as navigate, patch.object(browser, "status", wraps=browser.status) as show:
            with patch("player.runtime.time.sleep", side_effect=[None, KeyboardInterrupt]):
                with self.assertRaises(KeyboardInterrupt):
                    run_player(browser, worker, self.state, self.server)
        self.assertEqual(navigate.call_count, 2)
        self.assertEqual([call.args[0] for call in show.call_args_list], [initial["name"], changed["name"]])
        status = (self.state / "status.html").read_text()
        self.assertIn("<h1>Reception &lt;East&gt; &amp; West</h1>", status)
        self.assertNotIn("Ready to display", status)
        self.assertIn("Your display is paired.", status)
        worker.stopping.set.assert_called_once()

    def test_ready_screen_with_legacy_cache_keeps_previous_heading(self):
        config = {"state": "paired", "slides": []}
        write_json(self.state / "playlist.json", {"server": self.server, "config": config})
        worker = Mock()
        worker.updates.get_nowait.return_value = ("offline", "Offline")
        browser = Mock()
        browser.process.poll.return_value = None
        with patch("player.runtime.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_player(browser, worker, self.state, self.server)
        browser.status.assert_called_once_with(
            "Ready to display", "Your display is paired.\nAdd URLs or assets in your PiSign workspace.",
        )

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
                socket = Mock()
                socket.recv.side_effect = [
                    json.dumps({"id": 1, "result": {}}),
                    json.dumps({"id": 2, "result": {"identifier": "watermark"}}),
                ]
                with patch("websocket.create_connection", return_value=socket):
                    browser.start()
        arguments = popen.call_args.args[0]
        self.assertIn("--kiosk", arguments)
        self.assertIn("--remote-debugging-address=127.0.0.1", arguments)
        self.assertNotIn("--no-sandbox", arguments)
        self.assertEqual(arguments[-1], "about:blank")
        browser.close()

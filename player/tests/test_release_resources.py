import importlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import build_player_release


ROOT = Path(__file__).resolve().parents[2]


def load_updater():
    if os.name == "nt":
        with patch.dict(sys.modules, {"fcntl": types.ModuleType("fcntl")}):
            return importlib.import_module("player.updater")
    return importlib.import_module("player.updater")


class ReleaseResourceTests(unittest.TestCase):
    def assert_isolated_rendering(self, release):
        script = """
import base64, json, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from player.browser import Browser, watermark_script
state = Path(sys.argv[2])
with patch.object(Browser, "navigate"):
    Browser(state, executable="browser").status("Test", "Ready")
logo = (Path(sys.argv[1]) / "player" / "logo-dark.svg").read_bytes()
assert base64.b64encode(logo).decode("ascii") in (state / "status.html").read_text(encoding="utf-8")
assert '"tag": "svg"' in watermark_script()
print(json.dumps({"logo": len(logo), "module": __import__("player").__file__}))
"""
        with tempfile.TemporaryDirectory() as state:
            result = subprocess.run(
                [sys._base_executable, "-I", "-S", "-c", script, str(release), state],
                cwd=release, capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertGreater(report["logo"], 0)
        self.assertEqual(Path(report["module"]).resolve(), (release / "player" / "__init__.py").resolve())

    def test_player_logo_matches_canonical_brand(self):
        self.assertEqual(
            (ROOT / "player" / "logo-dark.svg").read_bytes(),
            (ROOT / "pisign" / "static" / "pisign" / "brand" / "logo-dark.svg").read_bytes(),
        )

    def test_bootstrap_copies_resources_and_checks_watermark(self):
        updater = load_updater()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, state, public = root / "updater", root / "state", root / "public.pem"
            state.mkdir()
            public.write_text("test public key", encoding="ascii")
            commands = []

            def run(command, **kwargs):
                commands.append((command, kwargs))
                return subprocess.CompletedProcess(command, 0)

            with patch.object(updater.subprocess, "run", side_effect=run), \
                    patch.object(updater.venv.EnvBuilder, "create"), \
                    patch.object(updater, "sync_tree"), patch.object(updater, "durable_write"):
                updater.bootstrap(home, state, "https://example.com", ["run"], public)

            release = home / "releases" / "bootstrap"
            self.assert_isolated_rendering(release)
            self.assertIn("watermark_script()", commands[-1][0][-1])

    def test_built_archive_works_with_existing_updater_and_without_checkout(self):
        updater = load_updater()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, extracted = root / "output", root / "extracted"
            extracted.mkdir()
            key = root / "private.pem"
            key.write_text("test private key", encoding="ascii")

            def run(command, **kwargs):
                if "download" in command:
                    wheel_dir = Path(command[command.index("--dest") + 1])
                    wheel_dir.mkdir()
                    for name in ("websocket_client-1.8.0", "segno-1.6.6"):
                        (wheel_dir / f"{name}-py3-none-any.whl").write_bytes(b"test wheel")
                else:
                    Path(command[command.index("-out") + 1]).write_bytes(b"signature")
                return subprocess.CompletedProcess(command, 0)

            argv = [
                "build_player_release", "--version", "1.0.0", "--sequence", "1",
                "--machines", "aarch64", "--python", "3.11",
                "--private-key", str(key), "--output", str(output),
            ]
            with patch.object(sys, "argv", argv), patch.object(build_player_release.subprocess, "run", side_effect=run):
                build_player_release.main()
            archive = output / "player-1.tar.gz"
            with tarfile.open(archive, "r:gz") as source:
                self.assertIn("player/logo-dark.svg", source.getnames())
                self.assertNotIn("pisign/static/pisign/brand/logo-dark.svg", source.getnames())
            updater.extract_release(archive, extracted)
            self.assert_isolated_rendering(extracted)

            # Missing bundled resources must fail instead of finding a checkout package.
            (extracted / "player" / "logo-dark.svg").unlink()
            script = "from player.browser import watermark_script; watermark_script()"
            result = subprocess.run(
                [sys._base_executable, "-I", "-S", "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]); " + script, str(extracted)],
                cwd=extracted, capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("logo-dark.svg", result.stderr)

    def test_staging_checks_bundled_watermark_before_activation(self):
        updater = load_updater()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, state = root / "updater", root / "state"
            (home / "releases").mkdir(parents=True)
            state.mkdir()
            (home / "config.json").write_text(json.dumps({"state_dir": str(state)}), encoding="utf-8")
            (home / "record.json").write_text("{}", encoding="utf-8")
            supervisor = updater.Supervisor(home)
            commands = []

            def run(command, **kwargs):
                commands.append(command)
                return subprocess.CompletedProcess(command, 0)

            manifest = {"sequence": 1, "sha256": "a" * 64}

            def download(_manifest, archive):
                archive.touch()

            with patch.object(supervisor, "download", side_effect=download), \
                    patch.object(updater, "extract_release"), \
                    patch.object(updater.venv.EnvBuilder, "create"), \
                    patch.object(updater.subprocess, "run", side_effect=run), \
                    patch.object(updater, "sync_tree"):
                supervisor.stage(manifest)
            self.assertIn("watermark_script()", commands[-1][-1])


if __name__ == "__main__":
    unittest.main()

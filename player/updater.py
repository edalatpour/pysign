"""Independent, unprivileged supervisor. Installed separately from release code."""

import argparse
import ctypes
import fcntl
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import uuid
import venv
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

try:
    from .releases import PLAYER_REQUIREMENTS, PROTOCOL, STATE_SCHEMA, digest_file, verify
    from .storage import read_json, write_json
except ImportError:
    from releases import PLAYER_REQUIREMENTS, PROTOCOL, STATE_SCHEMA, digest_file, verify
    from storage import read_json, write_json

logger = logging.getLogger("pisign-updater")
HEALTH_TIMEOUT = 120
HEALTH_WINDOW = 30


def parent_death_signal(parent_pid):
    # Linux clears this setting across fork, so set it in the player just before exec.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot set player parent-death signal.")
    if os.getppid() != parent_pid:
        os._exit(1)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_write(path, data):
    write_json(path, data)
    sync_directory(path.parent)


def sync_tree(root):
    for directory, _, names in os.walk(root, topdown=False):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        sync_directory(directory)
    sync_directory(root.parent)


def extract_release(archive, target):
    total = 0
    names = set()
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute() or ".." in path.parts or member.name in names
                or not member.isfile() or not path.parts
                or path.parts[0] not in ("player", "wheels", "requirements.txt")
                or (path.parts[0] == "requirements.txt" and len(path.parts) != 1)
            ):
                raise ValueError("Unsafe release archive entry.")
            names.add(member.name)
            total += member.size
            if total > 512 * 1024 * 1024 or len(names) > 4096:
                raise ValueError("Expanded release exceeds limits.")
            destination = target.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as content, destination.open("xb") as output:
                shutil.copyfileobj(content, output)
    if not (target / "player/__main__.py").is_file() or not (target / "requirements.txt").is_file():
        raise ValueError("Release is missing player code or pinned dependencies.")
    lines = (target / "requirements.txt").read_text(encoding="ascii").splitlines()
    if not lines or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9_.+!-]+ --hash=sha256:[a-f0-9]{64}", line)
        for line in lines
    ):
        raise ValueError("Release requirements must contain only exact versions with SHA-256 hashes.")


class Supervisor:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.config = read_json(self.home / "config.json")
        self.state = Path(self.config["state_dir"])
        self.record = read_json(self.home / "record.json")
        if not isinstance(self.record, dict):
            raise ValueError("Missing updater journal; rerun bootstrap.")
        self.child = None
        self.stopping = False
        self.health_id = None

    def save(self):
        durable_write(self.home / "record.json", self.record)

    def status(self, status, error="", target=None):
        report = {
            "installed_version": self.record["version"],
            "installed_sequence": self.record["sequence"],
            "status": status, "error": error[:1000],
            "target_version": target or self.record["version"],
            "updater_protocol": PROTOCOL,
        }
        self.record["report"] = report
        self.save()
        durable_write(self.state / "update-status.json", report)

    def recover(self):
        pending = self.record.get("pending")
        if pending:
            self.rollback("Startup was interrupted before health confirmation.")
        elif self.record.get("report", {}).get("status") == "staging":
            self.record["retry_at"] = time.time() + 60
            self.status("retrying", "Staging was interrupted before activation. Retrying in 60 seconds.",
                        self.record["report"]["target_version"])
        elif self.record.get("report"):
            durable_write(self.state / "update-status.json", self.record["report"])
        else:
            self.status("idle")

    def rollback(self, error):
        pending = self.record["pending"]
        self.record["failed"][str(pending["sequence"])] = error[:1000]
        self.record["pending"] = None
        self.save()
        self.status("rolled_back", error, pending["version"])

    def launch(self, release):
        self.health_id = uuid.uuid4().hex
        environment = dict(os.environ, PISIGN_HEALTH_ID=self.health_id,
                           PISIGN_UPDATE_HOME=str(self.home))
        parent_pid = os.getpid()
        self.child = subprocess.Popen(
            [str(self.home / "releases" / release / "venv/bin/python"),
             "-m", "player", *self.config["arguments"]],
            cwd=self.home / "releases" / release, env=environment, start_new_session=True,
            preexec_fn=lambda: parent_death_signal(parent_pid),
        )

    def stop_child(self):
        if self.child is None:
            return
        # Chromium inherits this process group, including across remote player exec.
        try:
            os.killpg(self.child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(self.child.pid, signal.SIGKILL)
            self.child.wait(timeout=5)
        self.child = None

    def healthy(self):
        receipt = read_json(self.state / "startup-health.json")
        return (
            self.child is not None and self.child.poll() is None
            and isinstance(receipt, dict) and receipt.get("id") == self.health_id
            and isinstance(receipt.get("time"), (float, int))
            and 0 <= time.monotonic() - receipt["time"] < 5
        )

    def download(self, manifest, archive):
        credentials = read_json(self.state / "credentials.json")
        if not credentials or credentials.get("server") != self.config["server"]:
            raise ValueError("Cannot download release without matching device credentials.")
        opener = build_opener(ProxyHandler({}), NoRedirects())
        request = Request(
            self.config["server"] + f"/api/device/releases/{manifest['sequence']}/",
            headers={"Authorization": f"Bearer {credentials['token']}"},
        )
        size = 0
        with opener.open(request, timeout=15) as response, archive.open("xb") as output:
            deadline = time.monotonic() + 300
            while chunk := response.read(64 * 1024):
                if self.stopping or time.monotonic() > deadline:
                    raise TimeoutError("Release download cancelled or timed out.")
                size += len(chunk)
                if size > manifest["size"]:
                    raise ValueError("Release download exceeds signed size.")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size != manifest["size"] or digest_file(archive) != manifest["sha256"]:
            raise ValueError("Release download failed integrity verification.")

    def stage(self, manifest):
        name = f"{manifest['sequence']}-{manifest['sha256']}"
        target = self.home / "releases" / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
        archive = target / "release.tar.gz"
        self.download(manifest, archive)
        extract_release(archive, target)
        archive.unlink()
        venv.EnvBuilder(with_pip=True).create(target / "venv")
        subprocess.run(
            [str(target / "venv/bin/python"), "-m", "pip", "--isolated", "install",
             "--no-index", "--only-binary=:all:", "--require-hashes",
             "--find-links", str(target / "wheels"), "-r", str(target / "requirements.txt")],
            check=True, timeout=180, cwd=target,
        )
        subprocess.run(
            [str(target / "venv/bin/python"), "-c",
             "import player.__main__, websocket, segno; "
             "from player.browser import watermark_script; watermark_script()"],
            check=True, timeout=30, cwd=target,
        )
        sync_tree(target)
        return name

    def update(self, envelope):
        manifest = verify(envelope, self.home / "release-public.pem")
        sequence = manifest["sequence"]
        if (
            sequence < self.record["highest"] or sequence <= self.record["sequence"]
            or str(sequence) in self.record["failed"]
            or (sequence == self.record["highest"] and time.time() < self.record.get("retry_at", 0))
        ):
            return
        # Persist anti-replay and failure suppression before any installation work.
        self.record["highest"] = sequence
        self.save()
        if (
            platform.machine() not in manifest["machines"]
            or f"{sys.version_info.major}.{sys.version_info.minor}" not in manifest["python"]
            or manifest["updater_protocol"] != PROTOCOL or manifest["state_schema"] != STATE_SCHEMA
        ):
            self.record["failed"][str(sequence)] = "Incompatible machine, Python, updater or state schema."
            self.save()
            self.status("incompatible", self.record["failed"][str(sequence)], manifest["version"])
            return
        self.status("staging", target=manifest["version"])
        try:
            name = self.stage(manifest)
        except (URLError, TimeoutError, ConnectionError, HTTPException) as exc:
            self.record["retry_at"] = time.time() + 60
            self.status("retrying", f"Release download interrupted: {type(exc).__name__}. Retrying in 60 seconds.",
                        manifest["version"])
            logger.warning("Release download interrupted; current player remains running: %s", type(exc).__name__)
            return
        except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
            self.record["failed"][str(sequence)] = f"Staging failed: {type(exc).__name__}: {exc}"[:1000]
            self.save()
            self.status("failed", self.record["failed"][str(sequence)], manifest["version"])
            logger.exception("Release staging failed; current player remains running.")
            return
        if self.stopping:
            self.record["retry_at"] = time.time() + 60
            self.status("retrying", "Staging stopped before activation. Retrying after restart.",
                        manifest["version"])
            return
        self.record["pending"] = {
            "release": name, "sequence": sequence, "version": manifest["version"],
        }
        self.save()
        self.status("starting", target=manifest["version"])
        self.stop_child()
        if self.stopping:
            return
        try:
            self.launch(name)
            deadline, ready_since = time.monotonic() + HEALTH_TIMEOUT, None
            while not self.stopping and time.monotonic() < deadline:
                if self.child.poll() is not None:
                    break
                if self.healthy():
                    if ready_since is None:
                        ready_since = time.monotonic()
                    if time.monotonic() - ready_since >= HEALTH_WINDOW:
                        self.record.update(
                            current=name, sequence=sequence, version=manifest["version"], pending=None,
                        )
                        self.save()
                        self.status("installed")
                        return
                else:
                    ready_since = None
                time.sleep(1)
        except (OSError, ValueError, subprocess.SubprocessError):
            logger.exception("Could not launch candidate release.")
        self.stop_child()
        self.rollback("Candidate failed startup health checks; previous release restored.")
        if not self.stopping:
            self.launch(self.record["current"])

    def run(self):
        self.recover()
        self.launch(self.record["current"])
        last_envelope = None
        try:
            while not self.stopping:
                if self.child.poll() is not None:
                    logger.error("Player exited; restarting the installed release.")
                    self.stop_child()
                    time.sleep(5)
                    if self.stopping:
                        break
                    self.launch(self.record["current"])
                envelope = read_json(self.state / "desired-release.json")
                retry_due = (
                    self.record.get("report", {}).get("status") == "retrying"
                    and time.time() >= self.record.get("retry_at", 0)
                )
                if envelope is not None and (envelope != last_envelope or retry_due):
                    try:
                        self.update(envelope)
                    except (ValueError, OSError, subprocess.SubprocessError) as exc:
                        logger.exception("Rejected release metadata.")
                        self.status("failed", f"Release metadata rejected: {exc}")
                    last_envelope = envelope
                time.sleep(1)
        finally:
            self.stop_child()


def bootstrap(home, state_dir, server, arguments, public_key):
    home, state_dir = Path(home).resolve(), Path(state_dir).resolve()
    if home == state_dir or home in state_dir.parents or state_dir in home.parents:
        raise ValueError("Updater and player state directories must be separate.")
    if (home / "config.json").exists():
        raise ValueError("Updater is already installed. Do not overwrite its trust key or journal.")
    if not server.startswith("https://"):
        raise ValueError("Remote updates require HTTPS.")
    subprocess.run(["openssl", "pkey", "-pubin", "-in", str(public_key), "-noout"], check=True, timeout=15)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    release = home / "releases/bootstrap"
    release.mkdir(parents=True)
    try:
        shutil.copytree(Path(__file__).parent, release / "player", ignore=shutil.ignore_patterns("__pycache__", "tests"))
        venv.EnvBuilder(with_pip=True).create(release / "venv")
        subprocess.run(
            [str(release / "venv/bin/python"), "-m", "pip", "--isolated", "install",
             "--only-binary=:all:", "--no-deps", *PLAYER_REQUIREMENTS], check=True, timeout=180,
        )
        subprocess.run(
            [str(release / "venv/bin/python"), "-c",
             "import player.__main__, websocket, segno; "
             "from player.browser import watermark_script; watermark_script()"],
            cwd=release, check=True, timeout=30,
        )
    except (OSError, shutil.Error, subprocess.SubprocessError):
        shutil.rmtree(release)
        raise
    for name in ("updater.py", "releases.py", "storage.py"):
        shutil.copyfile(Path(__file__).with_name(name), home / name)
    shutil.copyfile(public_key, home / "release-public.pem")
    sync_tree(home)
    durable_write(home / "record.json", {
        "current": "bootstrap", "sequence": 0, "version": "bootstrap",
        "highest": 0, "pending": None, "failed": {},
    })
    durable_write(home / "config.json", {
        "server": server, "state_dir": str(state_dir), "arguments": arguments,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    with (args.home / "updater.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another PiSign supervisor is running.")
        supervisor = Supervisor(args.home)

        def stop(signum, frame):
            supervisor.stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        supervisor.run()


if __name__ == "__main__":
    main()

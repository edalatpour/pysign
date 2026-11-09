import argparse
import fcntl
import logging
import os
import shlex
import shutil
import socket
import sys
from pathlib import Path

from .browser import Browser, BrowserError
from .client import ConfigWorker, ResetRequested, RestartRequested, command_receipt, server_url
from .runtime import run_player
from .storage import private_directory, reset_state


def parser():
    result = argparse.ArgumentParser(description="PiSign Raspberry Pi Chromium player")
    result.add_argument("command", choices=["run", "install-autostart", "reset"])
    result.add_argument("--server", help="Server origin, for example https://signage.example.com")
    result.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/pisign")
    result.add_argument("--name", default=socket.gethostname()[:100])
    result.add_argument("--browser", help="Chromium executable path")
    result.add_argument("--software-rendering", action="store_true",
                        help="Use CPU rendering for framebuffer/X11 kiosks (slower)")
    result.add_argument("--allow-http", action="store_true", help="Development only: allow an unencrypted server")
    result.add_argument("--yes", action="store_true", help="Confirm destructive local credential reset")
    return result


def install_autostart(args, server):
    executable = str(Path(sys.executable).absolute())
    arguments = [executable, "-m", "player", "run", "--server", server,
                 "--state-dir", str(args.state_dir.resolve()), "--name", args.name]
    if args.browser:
        arguments += ["--browser", args.browser]
    if args.software_rendering:
        arguments += ["--software-rendering"]
    if args.allow_http:
        arguments += ["--allow-http"]
    # Desktop Entry quoting is not shell quoting; escape its reserved characters.
    command = " ".join(
        '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
        .replace("$", "\\$").replace("%", "%%") + '"'
        for value in arguments
    )
    directory = Path.home() / ".config/autostart"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "pisign.desktop"
    target.write_text(
        "[Desktop Entry]\nType=Application\nName=PiSign Player\n"
        f"Exec={command}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    # labwc (current Raspberry Pi OS) uses a compositor autostart script.
    launcher = args.state_dir / "start-player"
    launcher.write_text(
        "#!/bin/sh\numask 077\n"
        f"exec {shlex.join(arguments)} >> {shlex.quote(str(args.state_dir / 'player.log'))} 2>&1\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    print(f"Installed desktop autostart: {target}")
    labwc = Path.home() / ".config/labwc"
    if labwc.exists() or shutil.which("labwc"):
        labwc.mkdir(parents=True, exist_ok=True)
        autostart = labwc / "autostart"
        line = f"{shlex.quote(str(launcher))} &"
        existing = autostart.read_text(encoding="utf-8") if autostart.exists() else ""
        if line not in existing.splitlines():
            autostart.write_text(existing.rstrip() + "\n" + line + "\n", encoding="utf-8")
        print(f"Installed labwc autostart: {autostart}")
    print("Enable desktop auto-login with sudo raspi-config. Run the player as this user, never root.")


def main():
    args = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    args.state_dir = private_directory(args.state_dir)
    restart = False
    with (args.state_dir / "player.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another player is running. Stop it before changing its state.")
        if args.command == "reset":
            if not args.yes:
                raise SystemExit("Reset discards local credentials and cached URLs. Confirm with --yes.")
            reset_state(args.state_dir, clear_identity=True)
            print("Local identity reset. Remove the old display in the web UI, then run to pair again.")
            return
        if not args.server:
            raise SystemExit("--server is required.")
        try:
            server = server_url(args.server, args.allow_http)
            if args.command == "install-autostart":
                install_autostart(args, server)
                return
            receipt = command_receipt(args.state_dir, server, "reset")
            if receipt is not None and not receipt["acknowledged"]:
                reset_state(args.state_dir)
            browser = Browser(args.state_dir, args.browser, software_rendering=args.software_rendering)
            worker = ConfigWorker(server, args.state_dir, args.name)
            run_player(browser, worker, args.state_dir, server)
        except RestartRequested:
            restart = True
        except ResetRequested:
            try:
                reset_state(args.state_dir)
            except OSError as exc:
                raise SystemExit(f"Player reset failed: {exc}") from exc
            restart = True
        except (BrowserError, OSError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        except KeyboardInterrupt:
            pass
    if restart:
        try:
            os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
        except OSError as exc:
            logging.exception("Could not re-execute the player.")
            raise SystemExit(f"Player restart failed: {exc}") from exc


if __name__ == "__main__":
    main()

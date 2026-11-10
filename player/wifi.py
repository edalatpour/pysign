"""Standalone boot-time Wi-Fi importer; installed as a root-owned system service."""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


BOOT_DIRECTORY = Path("/boot/firmware")
STATUS_DIRECTORY = Path("/run/pisign-wifi")
CONNECTION_DIRECTORY = Path("/etc/NetworkManager/system-connections")
NMCLI = "/usr/bin/nmcli"
RASPI_CONFIG = "/usr/bin/raspi-config"
FAILURE_MESSAGE = (
    "Wi-Fi setup failed. Shut down the Pi safely and check pisign-wifi.json "
    "and pisign-wifi-status.txt on the SD card's boot partition."
)


class WifiError(Exception):
    pass


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WifiError("Duplicate JSON fields are not allowed.")
        result[key] = value
    return result


def read_configuration(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise WifiError("pisign-wifi.json must be a regular file.")
        raw = handle.read(4097)
    if len(raw) > 4096:
        raise WifiError("pisign-wifi.json must be at most 4096 bytes.")
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_fields)
    except (UnicodeError, ValueError, RecursionError):
        raise WifiError("pisign-wifi.json must contain valid UTF-8 JSON.") from None
    if (
        not isinstance(data, dict)
        or not {"ssid", "password", "country"} <= data.keys()
        or data.keys() - {"ssid", "password", "country", "hidden"}
    ):
        raise WifiError("Use only ssid, password, country, and optional hidden fields.")
    ssid, password, country = data["ssid"], data["password"], data["country"]
    try:
        valid_ssid = isinstance(ssid, str) and 1 <= len(ssid.encode("utf-8")) <= 32 and "\0" not in ssid
    except UnicodeError:
        valid_ssid = False
    if not valid_ssid:
        raise WifiError("ssid must contain 1 to 32 UTF-8 bytes without NUL characters.")
    if not isinstance(password, str) or not (
        re.fullmatch(r"[\x20-\x7e]{8,63}", password)
        or re.fullmatch(r"[0-9a-fA-F]{64}", password)
    ):
        raise WifiError("password must be 8 to 63 printable ASCII characters or 64 hexadecimal digits.")
    if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
        raise WifiError("country must be an uppercase two-letter country code.")
    if type(data.get("hidden", False)) is not bool:
        raise WifiError("hidden must be true or false.")
    return {**data, "hidden": data.get("hidden", False)}


def keyfile(config, connection_id):
    # Byte-array SSIDs and GLib string escaping prevent keyfile syntax injection.
    ssid = ";".join(str(byte) for byte in config["ssid"].encode("utf-8")) + ";"
    password = config["password"].replace("\\", "\\\\").replace(" ", "\\s")
    return (
        "[connection]\n"
        f"id=PiSign Wi-Fi {connection_id}\nuuid={connection_id}\n"
        "type=wifi\ninterface-name=wlan0\nautoconnect=false\n"
        "[wifi]\nmode=infrastructure\n"
        f"ssid={ssid}\nhidden={'true' if config['hidden'] else 'false'}\n"
        "[wifi-security]\nkey-mgmt=wpa-psk\n"
        f"psk={password}\n"
        "[ipv4]\nmethod=auto\n"
        "[ipv6]\nmethod=auto\n"
    )


def command(arguments, action, timeout=20):
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=timeout, check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired:
        raise WifiError(f"{action} timed out.") from None
    except OSError:
        raise WifiError(f"{action} could not be started.") from None
    if result.returncode:
        # Network tools can echo secrets in diagnostics; never forward their output.
        raise WifiError(f"{action} failed (exit code {result.returncode}).")
    return result.stdout.strip()


def configure(config, connection_directory, source):
    previous = command(
        [NMCLI, "--get-values", "GENERAL.CON-UUID", "device", "show", "wlan0"],
        "Checking the Wi-Fi adapter",
    )
    if previous in ("", "--"):
        previous = None
    else:
        try:
            previous = str(uuid.UUID(previous))
        except ValueError:
            raise WifiError("NetworkManager returned an invalid active connection ID.") from None
    command(
        [RASPI_CONFIG, "nonint", "do_wifi_country", config["country"]],
        "Setting the Wi-Fi country",
    )
    command([NMCLI, "radio", "wifi", "on"], "Enabling Wi-Fi")
    connection_id = str(uuid.uuid4())
    fd, filename = tempfile.mkstemp(
        prefix=f"pisign-{connection_id}-", suffix=".nmconnection", dir=connection_directory,
    )
    path = Path(filename)
    loaded = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(keyfile(config, connection_id))
            handle.flush()
            os.fsync(handle.fileno())
        command([NMCLI, "connection", "load", str(path)], "Loading the new Wi-Fi profile")
        loaded = True
        command(
            [NMCLI, "--wait", "60", "connection", "up", "uuid", connection_id, "ifname", "wlan0"],
            "Connecting to Wi-Fi (check the SSID, password, signal, and network availability)",
            timeout=70,
        )
        command(
            [NMCLI, "connection", "modify", "uuid", connection_id,
             "connection.autoconnect", "yes", "connection.autoconnect-priority", "10"],
            "Saving Wi-Fi autoconnect settings",
        )
        source.unlink()
    except (WifiError, OSError):
        try:
            if loaded:
                command(
                    [NMCLI, "connection", "delete", "uuid", connection_id],
                    "Removing the unsuccessful Wi-Fi profile",
                )
            path.unlink(missing_ok=True)
            if previous:
                command(
                    [NMCLI, "--wait", "30", "connection", "up", "uuid", previous, "ifname", "wlan0"],
                    "Restoring the previous Wi-Fi connection", timeout=40,
                )
        except (WifiError, OSError):
            raise WifiError(
                "Wi-Fi import failed and cleanup or reconnection also failed. "
                "Existing saved profiles have not been deleted; use Ethernet or local recovery."
            ) from None
        raise


def provision(
    boot_directory=BOOT_DIRECTORY,
    status_directory=STATUS_DIRECTORY,
    connection_directory=CONNECTION_DIRECTORY,
):
    source = boot_directory / "pisign-wifi.json"
    # lexists also catches broken symlinks, which must be rejected rather than ignored.
    if not os.path.lexists(source):
        return 0
    status_directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    status_directory.chmod(0o755)
    failed = status_directory / "failed"
    status = boot_directory / "pisign-wifi-status.txt"
    try:
        config = read_configuration(source)
        configure(config, connection_directory, source)
    except (WifiError, OSError) as exc:
        message = str(exc) if isinstance(exc, WifiError) else "Cannot read, save, or remove Wi-Fi configuration files."
        failed.touch(mode=0o644)
        status.write_text(f"ERROR: {message}\n{FAILURE_MESSAGE}\n", encoding="utf-8")
        print(f"PiSign: {message}\n{FAILURE_MESSAGE}", file=sys.stderr)
        return 1
    failed.unlink(missing_ok=True)
    status.write_text(
        "SUCCESS: Wi-Fi connected and saved. pisign-wifi.json was removed.\n"
        "This confirms a local network connection, not access to the internet or PiSign server.\n",
        encoding="utf-8",
    )
    print("PiSign: Wi-Fi connected and saved; import file removed.")
    return 0


def main():
    os.umask(0o077)
    if os.geteuid() != 0:
        print("PiSign Wi-Fi import must run through its root-owned system service.", file=sys.stderr)
        return 1
    try:
        return provision()
    except OSError:
        print("PiSign: could not write Wi-Fi setup status. Check filesystem permissions and free space.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

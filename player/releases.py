"""Release protocol shared by the publisher, server and standalone supervisor."""

import base64
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

PROTOCOL = 1
STATE_SCHEMA = 1
MAX_ARCHIVE = 256 * 1024 * 1024
PLAYER_REQUIREMENTS = ("websocket-client==1.8.0", "segno==1.6.6")


def canonical(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def validate_manifest(manifest):
    fields = {
        "sequence", "version", "sha256", "size", "machines", "python",
        "updater_protocol", "state_schema",
    }
    if not isinstance(manifest, dict) or set(manifest) != fields:
        raise ValueError("Invalid release manifest fields.")
    if type(manifest["sequence"]) is not int or not 1 <= manifest["sequence"] <= 2147483647:
        raise ValueError("Invalid release sequence.")
    if not isinstance(manifest["version"], str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", manifest["version"],
    ):
        raise ValueError("Invalid release version.")
    if len(manifest["version"]) > 64:
        raise ValueError("Release version is too long.")
    if not isinstance(manifest["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", manifest["sha256"]):
        raise ValueError("Invalid release digest.")
    if type(manifest["size"]) is not int or not 1 <= manifest["size"] <= MAX_ARCHIVE:
        raise ValueError("Invalid release size.")
    compatibility = (
        ("machines", r"(aarch64|armv7l|armv6l|x86_64)"),
        ("python", r"3\.(1[0-9]|[2-9][0-9])"),
    )
    for key, pattern in compatibility:
        values = manifest[key]
        if not isinstance(values, list) or not 1 <= len(values) <= 32 or any(
            not isinstance(value, str) or not re.fullmatch(pattern, value) for value in values
        ):
            raise ValueError(f"Invalid release {key} compatibility.")
    for key in ("updater_protocol", "state_schema"):
        if type(manifest[key]) is not int or manifest[key] < 1:
            raise ValueError(f"Invalid release {key}.")
    return manifest


def verify(envelope, public_key):
    if not isinstance(envelope, dict) or set(envelope) != {"manifest", "signature"}:
        raise ValueError("Invalid signed release envelope.")
    manifest = validate_manifest(envelope["manifest"])
    try:
        signature = base64.b64decode(envelope["signature"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid release signature encoding.") from exc
    if len(signature) != 64:
        raise ValueError("Invalid Ed25519 signature length.")
    with tempfile.TemporaryDirectory(prefix="pisign-signature-") as directory:
        path = Path(directory)
        (path / "manifest").write_bytes(canonical(manifest))
        (path / "signature").write_bytes(signature)
        result = subprocess.run(
            ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public_key),
             "-rawin", "-in", str(path / "manifest"), "-sigfile", str(path / "signature")],
            capture_output=True, timeout=15, check=False,
        )
    if result.returncode:
        raise ValueError("Release signature verification failed.")
    return manifest


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

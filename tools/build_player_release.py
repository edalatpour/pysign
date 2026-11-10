"""Build a self-contained, pinned player archive and sign its manifest offline."""

import argparse
import base64
import json
import platform
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from player.releases import PLAYER_REQUIREMENTS, canonical, digest_file, validate_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sequence", required=True, type=int)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--machines", nargs="+", default=[platform.machine()])
    parser.add_argument("--python", nargs="+", default=[f"{sys.version_info.major}.{sys.version_info.minor}"])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / f"player-{args.sequence}.tar.gz"
    envelope_path = args.output / f"player-{args.sequence}.json"
    if archive.exists() or envelope_path.exists():
        parser.error("Output release already exists; choose a new sequence or empty output directory.")
    with tempfile.TemporaryDirectory(prefix="pisign-release-") as directory:
        root = Path(directory)
        wheels = root / "wheels"
        subprocess.run(
            [sys.executable, "-m", "pip", "--isolated", "download", "--only-binary=:all:",
             "--no-deps", "--dest", str(wheels), *PLAYER_REQUIREMENTS], check=True,
        )
        files = sorted(wheels.glob("*.whl"))
        if len(files) != len(PLAYER_REQUIREMENTS) or any(
            not path.name.endswith("-py3-none-any.whl") for path in files
        ):
            raise ValueError("Protocol 1 requires exactly the pinned, platform-independent player wheels.")
        hashes = {}
        for path in files:
            name, version, *_ = path.name.split("-")
            hashes[f"{name.replace('_', '-')}=={version}"] = digest_file(path)
        if set(hashes) != set(PLAYER_REQUIREMENTS):
            raise ValueError("Downloaded wheels do not match the pinned player requirements.")
        requirements = root / "requirements.txt"
        requirements.write_text("".join(
            f"{requirement} --hash=sha256:{hashes[requirement]}\n"
            for requirement in PLAYER_REQUIREMENTS
        ), encoding="ascii")
        source = Path(__file__).resolve().parent.parent / "player"
        with tarfile.open(archive, "x:gz") as output:
            for path in sorted(source.iterdir()):
                if path.is_file() and (path.suffix == ".py" or path.name == "watermark.js"):
                    output.add(path, arcname=f"player/{path.name}", recursive=False)
            output.add(requirements, arcname="requirements.txt", recursive=False)
            for path in files:
                output.add(path, arcname=f"wheels/{path.name}", recursive=False)
        manifest = validate_manifest({
            "sequence": args.sequence, "version": args.version,
            "sha256": digest_file(archive), "size": archive.stat().st_size,
            "machines": args.machines, "python": args.python,
            "updater_protocol": 1, "state_schema": 1,
        })
        (root / "manifest").write_bytes(canonical(manifest))
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", str(args.private_key.resolve()),
             "-rawin", "-in", str(root / "manifest"), "-out", str(root / "signature")],
            check=True, timeout=15,
        )
        envelope_path.write_text(json.dumps({
            "manifest": manifest,
            "signature": base64.b64encode((root / "signature").read_bytes()).decode("ascii"),
        }, indent=2) + "\n", encoding="ascii")
    print(f"Built {archive} and {envelope_path}. Publish only after testing on supported Pi/OS combinations.")


if __name__ == "__main__":
    main()

import json
import os
import shutil
import tempfile
from pathlib import Path


def reset_state(state_dir, clear_identity=False):
    names = ["playlist.json", "status.html", "restart.json"]
    if clear_identity:
        names += ["credentials.json", "reset.json"]
    for name in names:
        (state_dir / name).unlink(missing_ok=True)
    for name in ("browser-profile", "assets"):
        path = state_dir / name
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)


def private_directory(path):
    path = Path(path).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def read_json(path):
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None


def write_json(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".pisign-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

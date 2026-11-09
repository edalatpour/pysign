import re
import shutil
import zipfile
from uuid import UUID

from .storage import private_directory

CONTENT_TYPES = {
    "image/jpeg": "image", "image/png": "image", "image/webp": "image",
    "video/mp4": "video", "video/webm": "video", "application/zip": "document",
}


def validate_asset(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid asset metadata.")
    identifier = value.get("id")
    if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
        raise ValueError("Invalid asset identifier.")
    content_type = value.get("content_type")
    size, digest, pages = value.get("size"), value.get("sha256"), value.get("page_count", 0)
    if (
        not isinstance(content_type, str) or content_type not in CONTENT_TYPES
        or type(size) is not int or not 1 <= size <= 512 * 1024 * 1024
        or not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest)
        or type(pages) is not int
        or (not 1 <= pages <= 50 if content_type == "application/zip" else pages != 0)
    ):
        raise ValueError("Invalid asset metadata.")
    return {"id": identifier, "content_type": content_type, "size": size, "sha256": digest, "page_count": pages}


def clear_assets(state_dir):
    path = state_dir / "assets"
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def prepare_assets(config, api, token, state_dir, stopping):
    directory = private_directory(state_dir / "assets")
    assets = {slide["asset"]["id"]: slide["asset"] for slide in config["slides"] if "asset" in slide}
    for path in directory.iterdir():
        if path.name not in assets:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
    for asset in assets.values():
        path = private_directory(directory / asset["id"])
        content = path / "content"
        if not content.is_file() or content.stat().st_size != asset["size"]:
            api.download_asset(asset, token, content, stopping)
        if asset["page_count"]:
            expected = [f"page-{number}.png" for number in range(1, asset["page_count"] + 1)]
            if all((path / name).is_file() for name in expected):
                continue
            try:
                with zipfile.ZipFile(content) as archive:
                    if archive.namelist() != expected or any(
                        info.compress_type != zipfile.ZIP_STORED or info.file_size > 16 * 1024 * 1024
                        for info in archive.infolist()
                    ):
                        raise ValueError("Invalid rendered document archive.")
                    for name in expected:
                        temporary = path / f".{name}.tmp"
                        with archive.open(name) as source, temporary.open("wb") as target:
                            shutil.copyfileobj(source, target, length=64 * 1024)
                        temporary.chmod(0o600)
                        temporary.replace(path / name)
            except zipfile.BadZipFile as exc:
                raise ValueError("Invalid rendered document archive.") from exc

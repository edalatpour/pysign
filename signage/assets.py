import hashlib
import json
import logging
import re
import subprocess
import tempfile
import warnings
import zipfile
from contextlib import contextmanager
from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.http import HttpResponse, StreamingHttpResponse
from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)
IMAGE_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
DOCUMENT_EXTENSIONS = ("pdf", "doc", "docx", "ppt", "pptx")


def convert_document(upload, suffix):
    import requests

    if not settings.DOCUMENT_CONVERTER_URL:
        raise ValidationError("Document conversion is not configured. Contact the administrator.")
    upload.seek(0)
    with requests.Session() as session:
        session.trust_env = False
        try:
            with session.post(
                settings.DOCUMENT_CONVERTER_URL + "/convert/" + suffix,
                data=upload, headers={"Content-Type": "application/octet-stream"},
                timeout=(5, 180), allow_redirects=False, stream=True,
            ) as response:
                if response.status_code == 422:
                    raise ValidationError("The document could not be converted. Check its format, size and page count.")
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValidationError("The document converter returned an unexpected response.")
                with tempfile.TemporaryFile() as output:
                    size = 0
                    for chunk in response.iter_content(64 * 1024):
                        size += len(chunk)
                        if size > settings.ASSET_MAX_BYTES:
                            raise ValidationError("The rendered document exceeds the asset size limit.")
                        output.write(chunk)
                    output.seek(0)
                    with zipfile.ZipFile(output) as archive:
                        names = archive.namelist()
                        if not 1 <= len(names) <= 50 or names != [
                            f"page-{number}.png" for number in range(1, len(names) + 1)
                        ] or any(
                            info.file_size > 16 * 1024 * 1024 or info.compress_type != zipfile.ZIP_STORED
                            for info in archive.infolist()
                        ):
                            raise ValidationError("The rendered document is invalid.")
                    output.seek(0)
                    upload.seek(0)
                    upload.truncate()
                    while chunk := output.read(64 * 1024):
                        upload.write(chunk)
                    upload.size = size
                    return len(names)
        except requests.RequestException as exc:
            logger.exception("Document converter request failed.")
            raise ValidationError("Document conversion is unavailable. Try again later.") from exc
        except zipfile.BadZipFile as exc:
            logger.error("Document converter returned an invalid archive.")
            raise ValidationError("The rendered document is invalid.") from exc


def validate_upload(upload):
    if upload.size == 0 or upload.size > settings.ASSET_MAX_BYTES:
        raise ValidationError(f"Files must be between 1 byte and {settings.ASSET_MAX_BYTES // (1024 * 1024)} MiB.")
    suffix = upload.name.rsplit(".", 1)[-1].lower()
    page_count = 0
    if suffix in DOCUMENT_EXTENSIONS:
        if upload.size > 100 * 1024 * 1024:
            raise ValidationError("Documents must be no larger than 100 MiB.")
        page_count = convert_document(upload, suffix)
        content_type = "application/zip"
    elif suffix in ("jpg", "jpeg", "png", "webp"):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(upload) as image:
                    image_format = image.format
                    if image_format not in IMAGE_FORMATS or image.width * image.height > 16_000_000:
                        raise ValidationError("Use a JPEG, PNG or WebP image up to 16 megapixels.")
                    if getattr(image, "n_frames", 1) != 1:
                        raise ValidationError("Animated images are not supported. Upload a video instead.")
                    image.load()
                    # Re-encode raster images so uploaded scripts/metadata are never served back.
                    with tempfile.TemporaryFile() as output:
                        ImageOps.exif_transpose(image).save(output, format=image_format)
                        size = output.tell()
                        if size > settings.ASSET_MAX_BYTES:
                            raise ValidationError("The decoded image exceeds the upload limit.")
                        output.seek(0)
                        upload.seek(0)
                        upload.truncate()
                        while chunk := output.read(64 * 1024):
                            upload.write(chunk)
                    upload.size = size
                    content_type = IMAGE_FORMATS[image_format]
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValidationError("The image is invalid or too large to decode.") from exc
    elif suffix in ("mp4", "webm"):
        # Uploads are disk-backed; no user-controlled URL or filename reaches ffprobe.
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-protocol_whitelist", "file",
                 "-format_whitelist", "mov,matroska,webm",
                 "-show_streams", "-show_format", "-of", "json", upload.temporary_file_path()],
                capture_output=True, timeout=20, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValidationError("Video inspection timed out. Use a smaller video.") from exc
        except FileNotFoundError as exc:
            logger.error("FFprobe is not installed; video uploads are unavailable.")
            raise ValidationError("Video uploads are not configured. Contact the administrator.") from exc
        if result.returncode:
            raise ValidationError("The video is invalid.")
        try:
            metadata = json.loads(result.stdout)
            streams = metadata["streams"]
            duration = float(metadata["format"]["duration"])
            formats = metadata["format"]["format_name"].split(",")
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError("The video metadata is invalid.") from exc
        expected_format = "mp4" if suffix == "mp4" else "webm"
        codecs = {"h264", "aac"} if suffix == "mp4" else {"vp8", "vp9", "opus", "vorbis"}
        videos = [stream for stream in streams if stream.get("codec_type") == "video"]
        if (
            expected_format not in formats or not 0 < duration <= 86400 or len(videos) != 1
            or any(stream.get("codec_type") not in ("video", "audio")
                   or stream.get("codec_name") not in codecs for stream in streams)
            or videos[0].get("width", 0) * videos[0].get("height", 0) > 3840 * 2160
            or not 1 <= max(videos[0].get("width", 0), videos[0].get("height", 0)) <= 3840
        ):
            raise ValidationError("Use MP4 (H.264/AAC) or WebM (VP8/VP9 with Opus/Vorbis), up to 4K and 24 hours.")
        content_type = "video/mp4" if suffix == "mp4" else "video/webm"
    else:
        raise ValidationError("Upload JPEG, PNG, WebP, MP4, WebM, PDF, Word or PowerPoint. HTML and SVG are not supported.")
    upload.seek(0)
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return content_type, upload.size, digest.hexdigest(), page_count


def local_storage():
    return FileSystemStorage(
        location=settings.ASSET_STORAGE_ROOT, file_permissions_mode=0o600, directory_permissions_mode=0o700,
    )


@lru_cache(maxsize=4)
def azure_container(account_url, container, client_id):
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import BlobServiceClient

    credential = ManagedIdentityCredential(client_id=client_id or None)
    return BlobServiceClient(account_url, credential=credential).get_container_client(container)


def blob(asset):
    return azure_container(
        settings.ASSET_AZURE_ACCOUNT_URL, settings.ASSET_AZURE_CONTAINER, settings.ASSET_AZURE_CLIENT_ID,
    ).get_blob_client(asset.storage_key)


def save_content(asset, upload):
    if settings.ASSET_AZURE_ACCOUNT_URL:
        from azure.storage.blob import ContentSettings

        blob(asset).upload_blob(
            upload, length=asset.size, overwrite=False,
            content_settings=ContentSettings(content_type=asset.content_type),
        )
    else:
        saved = local_storage().save(asset.storage_key, upload)
        if saved != asset.storage_key:
            local_storage().delete(saved)
            raise OSError("Asset storage key collision.")


def delete_content(asset):
    if settings.ASSET_AZURE_ACCOUNT_URL:
        blob(asset).delete_blob()
    else:
        local_storage().delete(asset.storage_key)


@contextmanager
def open_content(asset):
    if settings.ASSET_AZURE_ACCOUNT_URL:
        with tempfile.TemporaryFile() as handle:
            blob(asset).download_blob(length=asset.size).readinto(handle)
            handle.seek(0)
            yield handle
    else:
        with local_storage().open(asset.storage_key, "rb") as handle:
            yield handle


def document_page(asset, number):
    with open_content(asset) as handle, zipfile.ZipFile(handle) as archive:
        response = HttpResponse(archive.read(f"page-{number}.png"), content_type="image/png")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response


def content_response(request, asset):
    start, end, status = 0, asset.size - 1, 200
    value = request.headers.get("Range")
    if value:
        match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", value)
        if match and any(match.groups()):
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), end) if last else end
            else:
                start = max(0, asset.size - int(last))
            status = 206
        if not match or not any(match.groups()) or start > end or start >= asset.size or value == "bytes=-0":
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{asset.size}"
            response["Cache-Control"] = "no-store"
            return response
    length = end - start + 1
    if request.method == "HEAD":
        response = HttpResponse(content_type=asset.content_type, status=status)
    else:
        if settings.ASSET_AZURE_ACCOUNT_URL:
            chunks = blob(asset).download_blob(offset=start, length=length).chunks()
        else:
            def read_chunks():
                with local_storage().open(asset.storage_key, "rb") as handle:
                    handle.seek(start)
                    remaining = length
                    while remaining:
                        chunk = handle.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise OSError("Stored asset is truncated.")
                        remaining -= len(chunk)
                        yield chunk

            chunks = read_chunks()
        response = StreamingHttpResponse(chunks, content_type=asset.content_type, status=status)
    response["Content-Length"] = str(length)
    response["Accept-Ranges"] = "bytes"
    if status == 206:
        response["Content-Range"] = f"bytes {start}-{end}/{asset.size}"
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response["Content-Disposition"] = "inline"
    return response

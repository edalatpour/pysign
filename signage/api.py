import json
import logging
import secrets
from datetime import timedelta
from functools import wraps

from django.db import IntegrityError, transaction
from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods

from .assets import content_response
from .models import Asset, Device, PlayerRelease, PlayerReset, PlayerRestart
from .security import allow_request, digest_token

PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
logger = logging.getLogger(__name__)


def pairing_code():
    return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(10))


def device_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        device = None
        if scheme == "Bearer" and 32 <= len(token) <= 128:
            device = Device.objects.select_related("playlist").filter(token_digest=digest_token(token)).first()
        if device is None:
            return JsonResponse({"error": "Invalid device credential."}, status=401)
        request.device = device
        response = view(request, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response
    return wrapped


@csrf_exempt
@require_POST
def register(request):
    if not allow_request(request, "register", 30, 3600):
        return JsonResponse({"error": "Too many registrations. Try again later."}, status=429)
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "A JSON object is required."}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "A JSON object is required."}, status=400)
    name = data.get("name", "New display")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        return JsonResponse({"error": "Name must contain 1 to 100 characters."}, status=400)
    token = secrets.token_urlsafe(32)
    for _ in range(5):
        try:
            with transaction.atomic():
                device = Device.objects.create(
                    name=name.strip(), token_digest=digest_token(token),
                    pairing_code=pairing_code(),
                    pairing_expires_at=timezone.now() + timedelta(minutes=10),
                )
            break
        except IntegrityError:
            continue
    else:
        return JsonResponse({"error": "Could not allocate a pairing code. Retry later."}, status=503)
    response = JsonResponse({"device_id": str(device.pk), "token": token}, status=201)
    response["Cache-Control"] = "no-store"
    return response


@csrf_exempt
@require_http_methods(["GET", "POST"])
@device_required
def config(request):
    device = request.device
    updates = {"last_seen": timezone.now()}
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            report = data["update"]
            if not isinstance(report, dict) or set(report) != {
                "installed_version", "installed_sequence", "status", "error", "target_version", "updater_protocol",
            }:
                raise ValueError
            for key, limit in (("installed_version", 64), ("target_version", 64), ("error", 1000)):
                if not isinstance(report[key], str) or len(report[key]) > limit:
                    raise ValueError
            if report["status"] not in {
                "idle", "staging", "starting", "installed", "failed", "rolled_back", "incompatible", "retrying",
            }:
                raise ValueError
            if type(report["installed_sequence"]) is not int or not 0 <= report["installed_sequence"] <= 2147483647:
                raise ValueError
            if type(report["updater_protocol"]) is not int or report["updater_protocol"] != 1:
                raise ValueError
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            return JsonResponse({"error": "Invalid player update report."}, status=400)
        updates.update(player_update=report, update_reported_at=timezone.now())
    Device.objects.filter(pk=device.pk).update(**updates)
    release = PlayerRelease.objects.first()
    metadata = {"release": release.envelope} if release else {}
    if device.owner_id is None:
        return JsonResponse({"state": "unpaired", "slides": [], **metadata})
    if device.playlist_id and device.playlist.owner_id != device.owner_id:
        logger.error("Device %s has a playlist owned by a different account.", device.pk)
    slides = []
    if device.playlist_id and device.playlist.owner_id == device.owner_id:
        for slide in device.playlist.slides.select_related("asset"):
            item = {key: getattr(slide, key) for key in ("title", "url", "duration", "show_qr", "qr_url")}
            if slide.asset_id:
                if slide.asset.owner_id != device.owner_id:
                    logger.error("Slide %s references an asset owned by another account.", slide.pk)
                    continue
                item["asset"] = {
                    "id": str(slide.asset_id), "content_type": slide.asset.content_type,
                    "size": slide.asset.size, "sha256": slide.asset.sha256, "page_count": slide.asset.page_count,
                }
            slides.append(item)
    result = {
        "state": "paired", "name": device.name,
        "slides": slides,
        **metadata,
    }
    reset = PlayerReset.objects.filter(
        device=device, completed_at__isnull=True, expires_at__gt=timezone.now(),
    ).first()
    if reset is not None:
        result["reset"] = {
            "id": str(reset.request_id), "expires_at": reset.expires_at.isoformat(),
        }
        return JsonResponse(result)
    restart = PlayerRestart.objects.filter(
        device=device, completed_at__isnull=True, expires_at__gt=timezone.now(),
    ).first()
    if restart is not None:
        result["restart"] = {
            "id": str(restart.request_id), "expires_at": restart.expires_at.isoformat(),
        }
    return JsonResponse(result)


@require_http_methods(["GET", "HEAD"])
@device_required
def release_content(request, sequence):
    release = get_object_or_404(PlayerRelease, sequence=sequence)
    return content_response(request, release)


@require_http_methods(["GET", "HEAD"])
@device_required
def asset_content(request, asset_id):
    device = request.device
    if device.owner_id is None or not device.playlist_id or device.playlist.owner_id != device.owner_id:
        raise Http404
    asset = get_object_or_404(
        Asset.objects.filter(slides__playlist_id=device.playlist_id).distinct(),
        pk=asset_id, owner_id=device.owner_id,
    )
    return content_response(request, asset)


@csrf_exempt
@require_POST
@device_required
def restart_completed(request, request_id):
    now = timezone.now()
    restart = PlayerRestart.objects.filter(device=request.device, request_id=request_id).first()
    if restart is None or request.device.owner_id is None:
        return JsonResponse({"status": "obsolete"})
    if restart.completed_at is not None:
        return JsonResponse({"status": "completed"})
    if restart.expires_at <= now:
        return JsonResponse({"status": "expired"})
    updated = PlayerRestart.objects.filter(
        device=request.device, request_id=request_id, completed_at__isnull=True, expires_at__gt=now,
    ).update(completed_at=now)
    if not updated:
        return JsonResponse({"status": "obsolete"})
    logger.info("Player restart %s completed for device %s.", request_id, request.device.pk)
    return JsonResponse({"status": "completed"})


@csrf_exempt
@require_POST
@device_required
def reset_completed(request, request_id):
    with transaction.atomic():
        device = Device.objects.select_for_update().get(pk=request.device.pk)
        reset = PlayerReset.objects.select_for_update().filter(device=device, request_id=request_id).first()
        if reset is None:
            return JsonResponse({"status": "obsolete"})
        if reset.completed_at is not None:
            return JsonResponse({"status": "completed"})
        if device.owner_id is None:
            return JsonResponse({"status": "obsolete"})
        # Expiry limits delivery, not completion of an already executed reset.
        reset.completed_at = timezone.now()
        reset.save(update_fields=["completed_at"])
        device.owner = None
        device.playlist = None
        device.pairing_code = None
        device.pairing_expires_at = None
        device.save(update_fields=["owner", "playlist", "pairing_code", "pairing_expires_at"])
        PlayerRestart.objects.filter(device=device).delete()
    logger.info("Player reset %s completed and device %s unpaired.", request_id, device.pk)
    return JsonResponse({"status": "completed"})


@csrf_exempt
@require_POST
@device_required
def pairing(request):
    for _ in range(5):
        try:
            with transaction.atomic():
                device = Device.objects.get(pk=request.device.pk)
                if device.owner_id is not None:
                    return JsonResponse({"error": "This device is already paired."}, status=409)
                if not device.pairing_expires_at or device.pairing_expires_at <= timezone.now():
                    code = pairing_code()
                    expires = timezone.now() + timedelta(minutes=10)
                    # Do not recreate a code if the user claimed the device concurrently.
                    updated = Device.objects.filter(
                        pk=device.pk, owner__isnull=True,
                        pairing_expires_at=device.pairing_expires_at,
                    ).update(pairing_code=code, pairing_expires_at=expires)
                    if not updated:
                        continue
                    device.pairing_code, device.pairing_expires_at = code, expires
                return JsonResponse({
                    "code": device.pairing_code,
                    "expires_at": device.pairing_expires_at.isoformat(),
                })
        except IntegrityError:
            continue
    return JsonResponse({"error": "Pairing changed concurrently. Retry later."}, status=409)

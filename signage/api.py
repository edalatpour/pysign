import json
import secrets
from datetime import timedelta
from functools import wraps

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import Device
from .security import allow_request, digest_token

PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def pairing_code():
    return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(10))


def device_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        device = None
        if scheme == "Bearer" and 32 <= len(token) <= 128:
            device = Device.objects.filter(token_digest=digest_token(token)).first()
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


@require_GET
@device_required
def config(request):
    device = request.device
    Device.objects.filter(pk=device.pk).update(last_seen=timezone.now())
    if device.owner_id is None:
        return JsonResponse({"state": "unpaired", "slides": []})
    return JsonResponse({
        "state": "paired", "name": device.name,
        "slides": list(device.slides.values("title", "url", "duration")),
    })


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

import hashlib
import hmac
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import RateLimit


def digest_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def allow_request(request, scope, limit, seconds, identity=None):
    identity = identity if identity is not None else request.META.get("REMOTE_ADDR", "")
    key = hmac.new(
        settings.SECRET_KEY.encode(), f"{scope}:{identity}".encode(), hashlib.sha256,
    ).hexdigest()
    window = int(time.time()) // seconds
    with transaction.atomic():
        bucket, _ = RateLimit.objects.get_or_create(
            key=key, window=window,
            defaults={"expires_at": timezone.now() + timedelta(seconds=seconds * 2)},
        )
        RateLimit.objects.filter(pk=bucket.pk).update(count=F("count") + 1)
        bucket.refresh_from_db()
    return bucket.count <= limit

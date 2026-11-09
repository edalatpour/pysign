import uuid
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, URLValidator
from django.db import models
from django.utils import timezone


def validate_display_url(value):
    URLValidator(schemes=["http", "https"])(value)
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError("URLs must not contain embedded credentials.")


class Device(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="devices",
    )
    name = models.CharField(max_length=100, default="New display")
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    pairing_code = models.CharField(max_length=11, unique=True, null=True, editable=False)
    pairing_expires_at = models.DateTimeField(null=True, editable=False)
    last_seen = models.DateTimeField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "created_at"]

    @property
    def online(self):
        return self.last_seen is not None and self.last_seen > timezone.now() - timedelta(seconds=90)


class Slide(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="slides")
    title = models.CharField(max_length=100)
    url = models.URLField(max_length=2048, validators=[validate_display_url])
    duration = models.PositiveIntegerField(
        default=3600, validators=[MinValueValidator(1), MaxValueValidator(86400)],
    )
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]


class PlayerRestart(models.Model):
    device = models.OneToOneField(Device, on_delete=models.CASCADE, primary_key=True, related_name="restart")
    request_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    requested_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True)

    @property
    def status(self):
        if self.completed_at is not None:
            return "completed"
        if self.expires_at <= timezone.now():
            return "expired"
        return "pending"


class PlayerReset(models.Model):
    device = models.OneToOneField(Device, on_delete=models.CASCADE, primary_key=True, related_name="reset")
    request_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    requested_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True)

    @property
    def status(self):
        if self.completed_at is not None:
            return "completed"
        if self.expires_at <= timezone.now():
            return "expired"
        return "pending"


class RateLimit(models.Model):
    key = models.CharField(max_length=64)
    window = models.BigIntegerField()
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["key", "window"], name="unique_rate_window"),
        ]


class ExternalIdentity(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    issuer = models.CharField(max_length=512)
    subject = models.CharField(max_length=255)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["issuer", "subject"], name="unique_external_identity"),
        ]

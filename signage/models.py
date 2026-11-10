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


class Playlist(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="playlists",
    )
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="unique_owner_playlist_name"),
        ]

    def __str__(self):
        return self.name


class Device(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="devices",
    )
    name = models.CharField(max_length=100, default="New display")
    playlist = models.ForeignKey(
        Playlist, on_delete=models.RESTRICT, null=True, blank=True, related_name="devices",
    )
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    pairing_code = models.CharField(max_length=11, unique=True, null=True, editable=False)
    pairing_expires_at = models.DateTimeField(null=True, editable=False)
    last_seen = models.DateTimeField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    player_update = models.JSONField(default=dict, blank=True)
    update_reported_at = models.DateTimeField(null=True, editable=False)

    class Meta:
        ordering = ["name", "created_at"]

    @property
    def online(self):
        return self.last_seen is not None and self.last_seen > timezone.now() - timedelta(seconds=90)


class Asset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assets")
    name = models.CharField(max_length=100)
    content_type = models.CharField(max_length=32)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    page_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name

    @property
    def storage_key(self):
        return f"assets/{self.pk}"

    @property
    def is_video(self):
        return self.content_type.startswith("video/")


class PlayerRelease(models.Model):
    sequence = models.PositiveIntegerField(primary_key=True)
    version = models.CharField(max_length=64, unique=True)
    envelope = models.JSONField()
    content_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sequence"]

    @property
    def storage_key(self):
        return f"releases/{self.content_id}"

    @property
    def size(self):
        return self.envelope["manifest"]["size"]

    @property
    def content_type(self):
        return "application/gzip"


class Slide(models.Model):
    playlist = models.ForeignKey(Playlist, on_delete=models.CASCADE, related_name="slides")
    title = models.CharField(max_length=100)
    url = models.URLField(max_length=2048, blank=True, default="", validators=[validate_display_url])
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, null=True, blank=True, related_name="slides")
    show_qr = models.BooleanField(default=True)
    qr_url = models.URLField(max_length=2048, blank=True, default="", validators=[validate_display_url])
    duration = models.PositiveIntegerField(
        default=3600, validators=[MinValueValidator(1), MaxValueValidator(86400)],
    )
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(asset__isnull=True) & ~models.Q(url=""))
                | (models.Q(asset__isnull=False) & models.Q(url="")),
                name="slide_url_or_asset",
            ),
        ]

    def clean(self):
        super().clean()
        if bool(self.url) == bool(self.asset_id):
            raise ValidationError("Choose either a URL or an asset, not both.")
        if self.asset_id and self.playlist_id and self.asset.owner_id != self.playlist.owner_id:
            raise ValidationError({"asset": "Choose an asset from your own library."})


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

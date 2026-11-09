import logging
import uuid
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q, RestrictedError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .forms import ClaimForm, DeviceForm, PlaylistForm, SlideForm
from .models import Device, PlayerReset, PlayerRestart, Playlist, Slide
from .security import allow_request

logger = logging.getLogger(__name__)


@require_http_methods(["GET"])
def signin(request):
    return render(request, "signage/auth.html")


@login_required
def dashboard(request):
    return render(request, "signage/dashboard.html", {
        "devices": request.user.devices.select_related("playlist"),
    })


@login_required
@require_http_methods(["GET", "POST"])
def claim(request):
    form = ClaimForm(request.POST or None, initial={"code": request.GET.get("code", "")})
    if request.method == "POST":
        ip_allowed = allow_request(request, "claim-ip", 20, 300)
        user_allowed = allow_request(request, "claim-user", 10, 300, identity=str(request.user.pk))
        if not ip_allowed or not user_allowed:
            return render(request, "signage/claim.html", {
                "form": form, "error": "Too many attempts. Try again in five minutes.",
            }, status=429)
        if form.is_valid():
            # Conditional UPDATE makes claiming single-use even for concurrent requests.
            claimed = Device.objects.filter(
                owner__isnull=True, pairing_code=form.cleaned_data["code"],
                pairing_expires_at__gt=timezone.now(),
            ).update(
                owner=request.user, name=form.cleaned_data["name"],
                pairing_code=None, pairing_expires_at=None,
            )
            if claimed:
                messages.success(request, "Display paired. Select or create a playlist in its settings.")
                return redirect("dashboard")
            form.add_error("code", "This code is invalid, expired, or already used.")
    return render(request, "signage/claim.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def device_detail(request, device_id):
    device = get_object_or_404(Device.objects.select_for_update(), pk=device_id, owner=request.user)
    form = DeviceForm(request.POST if request.method == "POST" else None, instance=device, owner=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Display settings saved.")
        return redirect("device", device_id=device.pk)
    return render(request, "signage/device.html", {
        "device": device, "form": form,
        "restart": PlayerRestart.objects.filter(device=device).first(),
        "reset": PlayerReset.objects.filter(device=device).first(),
    })


@login_required
@require_POST
def restart_player(request, device_id):
    return queue_player_command(request, device_id, PlayerRestart, "restart")


@login_required
@require_POST
def reset_player(request, device_id):
    get_object_or_404(Device, pk=device_id, owner=request.user)
    if request.POST.get("confirm") != "on":
        messages.error(request, "Confirm deletion of the cached playlist and browser sessions before resetting.")
        return redirect("device", device_id=device_id)
    return queue_player_command(request, device_id, PlayerReset, "reset")


@transaction.atomic
def queue_player_command(request, device_id, model, command):
    device = get_object_or_404(Device.objects.select_for_update(), pk=device_id, owner=request.user)
    now = timezone.now()
    values = {
        "request_id": uuid.uuid4(), "requested_by": request.user,
        "requested_at": now, "expires_at": now + timedelta(minutes=10), "completed_at": None,
    }
    _, created = model.objects.get_or_create(device=device, defaults=values)
    # A conditional update prevents repeated clicks from scheduling multiple commands.
    queued = created or model.objects.filter(device=device).filter(
        Q(expires_at__lte=now) | Q(completed_at__isnull=False),
    ).update(**values)
    if queued:
        logger.info("Player %s %s requested for device %s by user %s.",
                    command, values["request_id"], device.pk, request.user.pk)
        if command == "reset":
            messages.success(request, "Reset/unpair requested. The display will leave your account after resetting.")
        else:
            messages.success(request, "Player restart requested. Waiting for the display to reconnect.")
    else:
        messages.info(request, f"A player {command} is already pending for this display.")
    return redirect("device", device_id=device.pk)


@login_required
@require_POST
def remove_device(request, device_id):
    device = get_object_or_404(Device, pk=device_id, owner=request.user)
    device.delete()
    messages.success(request, "Display removed and its device credential revoked.")
    return redirect("dashboard")


@login_required
def playlists(request):
    return render(request, "signage/playlists.html", {
        "playlists": request.user.playlists.annotate(
            slide_count=Count("slides", distinct=True), device_count=Count("devices", distinct=True),
        ),
    })


@login_required
@require_http_methods(["GET", "POST"])
def create_playlist(request, device_id=None):
    device = get_object_or_404(Device, pk=device_id, owner=request.user) if device_id else None
    form = PlaylistForm(request.POST if request.method == "POST" else None, owner=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                playlist = form.save()
                if device is not None:
                    device = get_object_or_404(
                        Device.objects.select_for_update(), pk=device.pk, owner=request.user,
                    )
                    device.playlist = playlist
                    device.save(update_fields=["playlist"])
        except IntegrityError:
            if not request.user.playlists.filter(name=form.cleaned_data["name"]).exists():
                raise
            form.add_error("name", "You already have a playlist with this name.")
        else:
            messages.success(request, "Playlist created. Add its first URL below.")
            return redirect("playlist", playlist_id=playlist.pk)
    return render(request, "signage/playlist_new.html", {"form": form, "device": device})


@login_required
@require_http_methods(["GET", "POST"])
def playlist_detail(request, playlist_id):
    playlist = get_object_or_404(Playlist, pk=playlist_id, owner=request.user)
    form = PlaylistForm(
        request.POST if request.method == "POST" else None, instance=playlist, owner=request.user,
    )
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                form.save()
        except IntegrityError:
            if not request.user.playlists.filter(name=form.cleaned_data["name"]).exclude(pk=playlist.pk).exists():
                raise
            form.add_error("name", "You already have a playlist with this name.")
        else:
            messages.success(request, "Playlist renamed.")
            return redirect("playlist", playlist_id=playlist.pk)
    next_position = (playlist.slides.aggregate(value=Max("position"))["value"] or 0) + 1
    return render(request, "signage/playlist.html", {
        "playlist": playlist, "form": form, "slide_form": SlideForm(initial={"position": next_position}),
    })


@login_required
@require_POST
def remove_playlist(request, playlist_id):
    playlist = get_object_or_404(Playlist, pk=playlist_id, owner=request.user)
    if request.POST.get("confirm") != "on":
        messages.error(request, "Confirm deletion of this playlist and its URLs.")
        return redirect("playlist", playlist_id=playlist.pk)
    try:
        with transaction.atomic():
            playlist.delete()
    except RestrictedError:
        messages.error(request, "Assign another playlist or clear it from all displays before deleting this playlist.")
        return redirect("playlist", playlist_id=playlist.pk)
    messages.success(request, "Playlist deleted.")
    return redirect("playlists")


@login_required
@require_http_methods(["GET", "POST"])
def edit_slide(request, playlist_id, slide_id=None):
    playlist = get_object_or_404(Playlist, pk=playlist_id, owner=request.user)
    slide = get_object_or_404(Slide, pk=slide_id, playlist=playlist) if slide_id else None
    form = SlideForm(request.POST if request.method == "POST" else None, instance=slide)
    if request.method == "POST" and form.is_valid():
        slide = form.save(commit=False)
        slide.playlist = playlist
        slide.save()
        messages.success(request, "Playlist saved.")
        return redirect("playlist", playlist_id=playlist.pk)
    return render(request, "signage/slide.html", {"form": form, "playlist": playlist})


@login_required
@require_POST
def remove_slide(request, playlist_id, slide_id):
    playlist = get_object_or_404(Playlist, pk=playlist_id, owner=request.user)
    get_object_or_404(Slide, pk=slide_id, playlist=playlist).delete()
    messages.success(request, "URL removed.")
    return redirect("playlist", playlist_id=playlist.pk)

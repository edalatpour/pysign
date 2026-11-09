import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def migrate_playlists(apps, schema_editor):
    Device = apps.get_model("signage", "Device")
    Playlist = apps.get_model("signage", "Playlist")
    Slide = apps.get_model("signage", "Slide")
    alias = schema_editor.connection.alias
    devices = Device.objects.using(alias)
    slides = Slide.objects.using(alias)
    owner_ids = devices.filter(owner__isnull=False).order_by("owner_id").values_list("owner_id", flat=True).distinct()
    for owner_id in owner_ids.iterator():
        playlist = Playlist.objects.using(alias).create(owner_id=owner_id, name="default")
        position = 1
        for device in devices.filter(owner_id=owner_id).order_by("created_at", "id").iterator():
            for slide in slides.filter(device_id=device.pk).order_by("position", "id").iterator():
                slides.filter(pk=slide.pk).update(playlist_id=playlist.pk, position=position)
                position += 1
        devices.filter(owner_id=owner_id).update(playlist_id=playlist.pk)
    # Unpaired devices have no account to own a saved playlist.
    slides.filter(playlist__isnull=True).delete()


def restore_device_slides(apps, schema_editor):
    Device = apps.get_model("signage", "Device")
    Slide = apps.get_model("signage", "Slide")
    alias = schema_editor.connection.alias
    slides = Slide.objects.using(alias)
    fields = ("title", "url", "duration", "position", "show_qr", "qr_url")
    for device in Device.objects.using(alias).filter(
        owner__isnull=False, playlist__isnull=False,
    ).iterator():
        for values in slides.filter(playlist_id=device.playlist_id, device__isnull=True).values(*fields).iterator():
            slides.create(device_id=device.pk, playlist_id=device.playlist_id, **values)
    slides.filter(device__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("signage", "0006_slide_qr_url"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Playlist",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100)),
                ("owner", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name="playlists", to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "ordering": ["name", "id"],
                "constraints": [
                    models.UniqueConstraint(fields=("owner", "name"), name="unique_owner_playlist_name"),
                ],
            },
        ),
        migrations.AddField(
            model_name="device",
            name="playlist",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT,
                related_name="devices", to="signage.playlist",
            ),
        ),
        migrations.AddField(
            model_name="slide",
            name="playlist",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="slides", to="signage.playlist",
            ),
        ),
        migrations.AlterField(
            model_name="slide",
            name="device",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="slides", to="signage.device",
            ),
        ),
        migrations.RunPython(migrate_playlists, restore_device_slides),
        migrations.RemoveField(model_name="slide", name="device"),
        migrations.AlterField(
            model_name="slide",
            name="playlist",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="slides", to="signage.playlist",
            ),
        ),
    ]

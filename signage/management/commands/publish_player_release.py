import json
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from player.releases import digest_file, verify
from signage.assets import delete_content, save_content
from signage.models import PlayerRelease


class Command(BaseCommand):
    help = "Platform operator: verify and publish a signed player release to every enrolled device."

    def add_arguments(self, parser):
        parser.add_argument("envelope", type=Path)
        parser.add_argument("archive", type=Path)

    def handle(self, *args, **options):
        if not settings.PLAYER_RELEASE_PUBLIC_KEY:
            raise CommandError("Set PISIGN_PLAYER_RELEASE_PUBLIC_KEY to the trusted public key file.")
        archive = options["archive"]
        try:
            envelope = json.loads(options["envelope"].read_text(encoding="utf-8"))
            manifest = verify(envelope, settings.PLAYER_RELEASE_PUBLIC_KEY)
            if archive.stat().st_size != manifest["size"] or digest_file(archive) != manifest["sha256"]:
                raise ValueError("Release archive does not match signed size and digest.")
            if PlayerRelease.objects.filter(sequence__gte=manifest["sequence"]).exists():
                raise ValueError("Release sequence must be higher than every published release.")
            release = PlayerRelease(
                sequence=manifest["sequence"], version=manifest["version"], envelope=envelope,
            )
            with archive.open("rb") as source:
                save_content(release, File(source))
            try:
                with transaction.atomic():
                    release.save(force_insert=True)
            except IntegrityError:
                delete_content(release)
                raise
        except (OSError, ValueError, IntegrityError, subprocess.SubprocessError) as exc:
            raise CommandError(f"Release publication failed: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(
            f"Published {release.version} (sequence {release.sequence}) to all enrolled devices.",
        ))

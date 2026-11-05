from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from signage.models import Device, RateLimit


class Command(BaseCommand):
    help = "Remove expired rate limits and unclaimed registrations older than one day."

    def handle(self, *args, **options):
        limits, _ = RateLimit.objects.filter(expires_at__lt=timezone.now()).delete()
        devices, _ = Device.objects.filter(
            owner__isnull=True, created_at__lt=timezone.now() - timedelta(days=1),
        ).exclude(last_seen__gte=timezone.now() - timedelta(days=1)).delete()
        self.stdout.write(f"Removed {limits} rate limits and {devices} abandoned devices.")

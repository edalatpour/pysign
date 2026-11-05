import logging
from ipaddress import ip_address

from django.conf import settings
from django.db import DatabaseError, connection
from django.http import HttpResponseBadRequest, HttpResponseNotAllowed, JsonResponse

logger = logging.getLogger(__name__)


class HealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path not in {"/health/live/", "/health/ready/"}:
            return self.get_response(request)
        # ACA probes reach the container directly, without ingress headers or its host name.
        if request.method not in {"GET", "HEAD"}:
            return HttpResponseNotAllowed(["GET", "HEAD"])
        status = 200
        if request.path == "/health/ready/":
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
            except DatabaseError:
                logger.warning("Database readiness check failed.")
                status = 503
        response = JsonResponse({"status": "ok" if status == 200 else "unavailable"}, status=status)
        response["Cache-Control"] = "no-store"
        return response


class ContainerAppsProxyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.AZURE_CONTAINER_APPS:
            # ACA appends the only trustworthy address; earlier entries can be client-supplied.
            forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "").rsplit(",", 1)[-1].strip()
            try:
                request.META["REMOTE_ADDR"] = str(ip_address(forwarded))
            except ValueError:
                logger.warning("Rejected a request with missing or invalid ACA client IP.")
                return HttpResponseBadRequest("Invalid ingress client address.")
        return self.get_response(request)

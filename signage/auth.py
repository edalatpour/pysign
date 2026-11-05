import hashlib
import logging
import re
import uuid
from urllib.parse import urlsplit

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.django_client import OAuth
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST
from joserfc.errors import JoseError
from requests.exceptions import RequestException

from .models import ExternalIdentity
from .security import allow_request

logger = logging.getLogger(__name__)


def entra_client():
    try:
        tenant = str(uuid.UUID(settings.ENTRA_TENANT_ID))
    except ValueError as exc:
        raise ImproperlyConfigured("PYSIGN_ENTRA_TENANT_ID must be the external tenant UUID.") from exc
    if not re.fullmatch(r"[a-zA-Z0-9-]+", settings.ENTRA_SUBDOMAIN):
        raise ImproperlyConfigured("Set PYSIGN_ENTRA_SUBDOMAIN to the external tenant subdomain.")
    if not settings.ENTRA_CLIENT_ID or not settings.ENTRA_CLIENT_SECRET:
        raise ImproperlyConfigured("Set the Entra application client ID and client secret.")
    callback = urlsplit(settings.ENTRA_REDIRECT_URI)
    if not callback.netloc or callback.scheme not in (["http", "https"] if settings.DEBUG else ["https"]):
        raise ImproperlyConfigured("Configure an absolute HTTPS PYSIGN_ENTRA_REDIRECT_URI.")
    oauth = OAuth()
    return oauth.register(
        name="entra", client_id=settings.ENTRA_CLIENT_ID,
        client_secret=settings.ENTRA_CLIENT_SECRET,
        server_metadata_url=(
            f"https://{settings.ENTRA_SUBDOMAIN}.ciamlogin.com/"
            f"{tenant}/v2.0/.well-known/openid-configuration"
        ),
        client_kwargs={
            "scope": "openid profile email", "code_challenge_method": "S256",
            "default_timeout": 10,
        },
    )


def user_from_claims(claims):
    # Authlib validates the signature, issuer, audience, expiry and nonce first.
    issuer, subject = claims.get("iss"), claims.get("sub")
    if not isinstance(issuer, str) or not isinstance(subject, str) or not issuer or not subject:
        raise ValueError("Missing OIDC identity claims.")
    if len(issuer) > 512 or len(subject) > 255:
        raise ValueError("OIDC identity claims are too long.")
    tenant = claims.get("tid")
    if not isinstance(tenant, str) or tenant.lower() != settings.ENTRA_TENANT_ID.lower():
        raise ValueError("The identity belongs to a different tenant.")
    username = "entra_" + hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()
    with transaction.atomic():
        user, created = get_user_model().objects.get_or_create(username=username)
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        identity, _ = ExternalIdentity.objects.get_or_create(
            issuer=issuer, subject=subject, defaults={"user": user},
        )
    if not identity.user.is_active:
        raise ValueError("The account is disabled.")
    return identity.user


def auth_error(request, status=400):
    return render(request, "signage/auth.html", {
        "error": "Microsoft sign-in could not be completed. Please try again or contact the operator.",
    }, status=status)


@require_POST
def begin(request):
    if not allow_request(request, "login", 30, 300):
        return render(request, "signage/auth.html", {
            "error": "Too many sign-in attempts. Try again in five minutes.",
        }, status=429)
    destination = request.POST.get("next", "")
    request.session["auth_next"] = destination if url_has_allowed_host_and_scheme(
        destination, allowed_hosts={request.get_host()}, require_https=not settings.DEBUG,
    ) else "/"
    try:
        return entra_client().authorize_redirect(request, settings.ENTRA_REDIRECT_URI)
    except ImproperlyConfigured:
        logger.error("Entra External ID is not configured correctly.")
        return auth_error(request, 503)
    except (OAuthError, RequestException):
        logger.warning("Entra authorization endpoint unavailable.")
        return auth_error(request, 502)


@require_GET
def callback(request):
    try:
        token = entra_client().authorize_access_token(request)
        claims = token.get("userinfo")
        if not claims:
            raise ValueError("Missing validated ID token.")
        user = user_from_claims(claims)
    except ImproperlyConfigured:
        logger.error("Entra External ID is not configured correctly.")
        return auth_error(request, 503)
    except (OAuthError, JoseError, RequestException, ValueError):
        logger.warning("Entra callback rejected; identity or OAuth transaction was invalid.")
        return auth_error(request)
    destination = request.session.pop("auth_next", "/")
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    messages.success(request, "Signed in with Microsoft.")
    return redirect(destination)

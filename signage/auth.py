import hashlib
import logging
import re
import time
import uuid
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST
from joserfc import jwt
from joserfc.errors import ClaimError, JoseError
from joserfc.jwk import KeySet
from msal import ConfidentialClientApplication
from requests.exceptions import RequestException

from .models import ExternalIdentity
from .security import allow_request

logger = logging.getLogger(__name__)


class SignInError(ValueError):
    """A sign-in failure whose message contains no credentials or claim values."""


def entra_authority():
    try:
        tenant = str(uuid.UUID(settings.ENTRA_TENANT_ID))
    except ValueError as exc:
        raise ImproperlyConfigured("PISIGN_ENTRA_TENANT_ID must be the external tenant UUID.") from exc
    if not re.fullmatch(r"[a-zA-Z0-9-]+", settings.ENTRA_SUBDOMAIN):
        raise ImproperlyConfigured("Set PISIGN_ENTRA_SUBDOMAIN to the external tenant subdomain.")
    if not settings.ENTRA_CLIENT_ID or not settings.ENTRA_CLIENT_SECRET:
        raise ImproperlyConfigured("Set the Entra application client ID and client secret.")
    callback = urlsplit(settings.ENTRA_REDIRECT_URI)
    if not callback.netloc or callback.scheme not in (["http", "https"] if settings.DEBUG else ["https"]):
        raise ImproperlyConfigured("Configure an absolute HTTPS PISIGN_ENTRA_REDIRECT_URI.")
    return f"https://{settings.ENTRA_SUBDOMAIN}.ciamlogin.com/{tenant}"


def entra_client():
    return ConfidentialClientApplication(
        settings.ENTRA_CLIENT_ID,
        client_credential=settings.ENTRA_CLIENT_SECRET,
        authority=entra_authority(),
        instance_discovery=False,
        timeout=10,
        exclude_scopes=["offline_access"],
    )


def validated_claims(token, flow):
    # MSAL 1.39 does not validate ID tokens; verify independently before sign-in.
    response = requests.get(
        f"{entra_authority()}/v2.0/.well-known/openid-configuration", timeout=10,
    )
    response.raise_for_status()
    metadata = response.json()
    issuer, jwks_uri = metadata.get("issuer"), metadata.get("jwks_uri")
    if not isinstance(issuer, str) or not issuer or not isinstance(jwks_uri, str):
        raise SignInError("Missing OIDC issuer or signing key endpoint.")
    endpoint = urlsplit(jwks_uri)
    if endpoint.scheme != "https" or not endpoint.netloc or endpoint.username or endpoint.password:
        raise SignInError("Invalid OIDC signing key endpoint.")
    response = requests.get(jwks_uri, timeout=10)
    response.raise_for_status()
    keyset = KeySet.import_key_set(response.json())
    decoded = jwt.decode(token, keyset, algorithms=["RS256"])
    nonce = hashlib.sha256(flow["nonce"].encode("ascii")).hexdigest()
    jwt.JWTClaimsRegistry(
        leeway=120,
        iss={"essential": True, "value": issuer},
        sub={"essential": True},
        aud={"essential": True, "value": settings.ENTRA_CLIENT_ID},
        exp={"essential": True},
        iat={"essential": True},
        nonce={"essential": True, "value": nonce},
    ).validate(decoded.claims)
    audience = decoded.claims["aud"]
    authorized_party = decoded.claims.get("azp")
    if authorized_party is not None or isinstance(audience, list) and len(audience) > 1:
        if authorized_party != settings.ENTRA_CLIENT_ID:
            raise SignInError("Invalid OIDC authorized party.")
    return decoded.claims


def user_from_claims(claims):
    issuer, subject = claims.get("iss"), claims.get("sub")
    if not isinstance(issuer, str) or not isinstance(subject, str) or not issuer or not subject:
        raise SignInError("Missing OIDC identity claims.")
    if len(issuer) > 512 or len(subject) > 255:
        raise SignInError("OIDC identity claims are too long.")
    tenant = claims.get("tid")
    if not isinstance(tenant, str) or tenant.lower() != settings.ENTRA_TENANT_ID.lower():
        raise SignInError("Missing tenant claim or tenant does not match PISIGN_ENTRA_TENANT_ID.")
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
        raise SignInError("The account is disabled.")
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
    request.session.pop("auth_flow", None)
    request.session["auth_next"] = destination if url_has_allowed_host_and_scheme(
        destination, allowed_hosts={request.get_host()}, require_https=not settings.DEBUG,
    ) else "/"
    try:
        flow = entra_client().initiate_auth_code_flow(
            scopes=["email"], redirect_uri=settings.ENTRA_REDIRECT_URI, response_mode="query",
        )
        if "error" in flow or not flow.get("auth_uri"):
            raise ValueError("Entra authorization flow could not be started.")
        flow["started_at"] = time.time()
        request.session["auth_flow"] = flow
        return redirect(flow["auth_uri"])
    except ImproperlyConfigured as exc:
        logger.error("Entra External ID configuration rejected: %s", exc)
        return auth_error(request, 503)
    except (ValueError, RequestException):
        logger.warning("Entra authorization endpoint unavailable.")
        return auth_error(request, 502)


@require_GET
def callback(request):
    flow = request.session.pop("auth_flow", None)
    destination = request.session.pop("auth_next", "/")
    stage = "session flow"
    try:
        if not flow:
            raise SignInError("Missing OAuth session. Use the same browser and hostname for login and callback.")
        if time.time() - flow["started_at"] > 600:
            raise SignInError("OAuth session expired. Start a new sign-in.")
        stage = "client initialization"
        remote = entra_client()
        stage = "token exchange"
        token = remote.acquire_token_by_auth_code_flow(flow, request.GET.dict())
        if "error" in token:
            error = token["error"]
            error = error if isinstance(error, str) and re.fullmatch(r"[a-z_]{1,64}", error) else "unknown_error"
            codes = token.get("error_codes")
            codes = [str(code) for code in codes[:5] if type(code) is int] if isinstance(codes, list) else []
            raise SignInError(f"Entra returned {error}; AADSTS codes: {','.join(codes) or 'not supplied'}.")
        if not token.get("id_token"):
            raise SignInError("Token response contains no ID token.")
        stage = "ID token validation"
        claims = validated_claims(token["id_token"], flow)
        stage = "account identity"
        user = user_from_claims(claims)
    except ImproperlyConfigured as exc:
        logger.error("Entra External ID configuration rejected: %s", exc)
        return auth_error(request, 503)
    except SignInError as exc:
        logger.warning("Entra callback rejected at %s: %s", stage, exc)
        return auth_error(request)
    except ClaimError as exc:
        claim = exc.claim if exc.claim in {"iss", "sub", "aud", "exp", "iat", "nbf", "nonce"} else "required claims"
        logger.warning("Entra callback rejected at %s: %s (%s).", stage, type(exc).__name__, claim)
        return auth_error(request)
    except (JoseError, RequestException, ValueError, RuntimeError) as exc:
        # Library exception messages may contain tokens, nonces, or provider responses.
        logger.warning("Entra callback rejected at %s: %s.", stage, type(exc).__name__)
        return auth_error(request)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    messages.success(request, "Signed in with Microsoft.")
    return redirect(destination)

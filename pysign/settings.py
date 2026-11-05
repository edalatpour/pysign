import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get("PYSIGN_DEBUG", "0") == "1"
SECRET_KEY = os.environ.get("PYSIGN_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("Set PYSIGN_SECRET_KEY to a long random secret.")
    SECRET_KEY = "development-only-not-for-deployment"

ALLOWED_HOSTS = os.environ.get("PYSIGN_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
AZURE_CONTAINER_APPS = os.environ.get("PYSIGN_AZURE_CONTAINER_APPS", "0") == "1"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "signage",
]
MIDDLEWARE = [
    "pysign.middleware.HealthCheckMiddleware",
    "pysign.middleware.ContainerAppsProxyMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "pysign.urls"
WSGI_APPLICATION = "pysign.wsgi.application"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
DATABASE_ENGINE = os.environ.get("PYSIGN_DATABASE_ENGINE", "sqlite")
if DATABASE_ENGINE == "sqlite":
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("PYSIGN_DATABASE", str(BASE_DIR / "db.sqlite3")),
        "OPTIONS": {"timeout": 20},
    }}
elif DATABASE_ENGINE == "postgresql":
    database_values = {}
    for key in ("NAME", "HOST", "USER", "PASSWORD"):
        value = os.environ.get(f"PYSIGN_POSTGRES_{key}", "")
        if not value:
            raise ImproperlyConfigured(f"Set PYSIGN_POSTGRES_{key} when using PostgreSQL.")
        database_values[key] = value
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        **database_values,
        "PORT": os.environ.get("PYSIGN_POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "sslmode": os.environ.get("PYSIGN_POSTGRES_SSLMODE", "verify-full"),
            "connect_timeout": 5,
        },
    }}
    if os.environ.get("PYSIGN_POSTGRES_SSLROOTCERT"):
        DATABASES["default"]["OPTIONS"]["sslrootcert"] = os.environ["PYSIGN_POSTGRES_SSLROOTCERT"]
else:
    raise ImproperlyConfigured("PYSIGN_DATABASE_ENGINE must be sqlite or postgresql.")
ENTRA_TENANT_ID = os.environ.get("PYSIGN_ENTRA_TENANT_ID", "")
ENTRA_SUBDOMAIN = os.environ.get("PYSIGN_ENTRA_SUBDOMAIN", "")
ENTRA_CLIENT_ID = os.environ.get("PYSIGN_ENTRA_CLIENT_ID", "")
ENTRA_CLIENT_SECRET = os.environ.get("PYSIGN_ENTRA_CLIENT_SECRET", "")
ENTRA_REDIRECT_URI = os.environ.get(
    "PYSIGN_ENTRA_REDIRECT_URI", "http://localhost:8000/accounts/callback/" if DEBUG else "",
)
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage" if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ),
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
DATA_UPLOAD_MAX_MEMORY_SIZE = 16384
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
CSRF_TRUSTED_ORIGINS = [
    value for value in os.environ.get("PYSIGN_CSRF_TRUSTED_ORIGINS", "").split(",") if value
]
# Enable only when a trusted proxy strips client-supplied forwarding headers.
if AZURE_CONTAINER_APPS or os.environ.get("PYSIGN_TRUST_PROXY", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

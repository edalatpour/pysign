import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get("PISIGN_DEBUG", "0") == "1"
SECRET_KEY = os.environ.get("PISIGN_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("Set PISIGN_SECRET_KEY to a long random secret.")
    SECRET_KEY = "development-only-not-for-deployment"

ALLOWED_HOSTS = os.environ.get("PISIGN_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
AZURE_CONTAINER_APPS = os.environ.get("PISIGN_AZURE_CONTAINER_APPS", "0") == "1"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact_request_query": {"()": "pisign.logging.RedactRequestQuery"},
    },
    "formatters": {
        "django.server": {
            "()": "django.utils.log.ServerFormatter",
            "format": "[{server_time}] {message}",
            "style": "{",
        },
    },
    "handlers": {
        "django.server": {
            "class": "logging.StreamHandler",
            "formatter": "django.server",
        },
    },
    "loggers": {
        "django.server": {
            "handlers": ["django.server"],
            "filters": ["redact_request_query"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "signage",
]
MIDDLEWARE = [
    "pisign.middleware.HealthCheckMiddleware",
    "pisign.middleware.ContainerAppsProxyMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "pisign.urls"
WSGI_APPLICATION = "pisign.wsgi.application"
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
DATABASE_ENGINE = os.environ.get("PISIGN_DATABASE_ENGINE", "sqlite")
if DATABASE_ENGINE == "sqlite":
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("PISIGN_DATABASE", str(BASE_DIR / "db.sqlite3")),
        "OPTIONS": {"timeout": 20},
    }}
elif DATABASE_ENGINE == "postgresql":
    database_values = {}
    for key in ("NAME", "HOST", "USER", "PASSWORD"):
        value = os.environ.get(f"PISIGN_POSTGRES_{key}", "")
        if not value:
            raise ImproperlyConfigured(f"Set PISIGN_POSTGRES_{key} when using PostgreSQL.")
        database_values[key] = value
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        **database_values,
        "PORT": os.environ.get("PISIGN_POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "sslmode": os.environ.get("PISIGN_POSTGRES_SSLMODE", "verify-full"),
            "connect_timeout": 5,
        },
    }}
    if os.environ.get("PISIGN_POSTGRES_SSLROOTCERT"):
        DATABASES["default"]["OPTIONS"]["sslrootcert"] = os.environ["PISIGN_POSTGRES_SSLROOTCERT"]
elif DATABASE_ENGINE == "mssql":
    database_values = {}
    for key in ("NAME", "HOST", "CLIENT_ID"):
        value = os.environ.get(f"PISIGN_SQL_{key}", "")
        if not value:
            raise ImproperlyConfigured(f"Set PISIGN_SQL_{key} when using Azure SQL.")
        database_values[key] = value
    try:
        sql_query_timeout = int(os.environ.get("PISIGN_SQL_QUERY_TIMEOUT", "5"))
    except ValueError:
        raise ImproperlyConfigured("PISIGN_SQL_QUERY_TIMEOUT must be a positive integer in seconds.") from None
    if sql_query_timeout < 1:
        raise ImproperlyConfigured("PISIGN_SQL_QUERY_TIMEOUT must be a positive integer in seconds.")
    DATABASES = {"default": {
        "ENGINE": "pisign.azuresql",
        "NAME": database_values["NAME"],
        "HOST": database_values["HOST"],
        "PORT": "1433",
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "driver": "ODBC Driver 18 for SQL Server",
            "extra_params": "Encrypt=yes;TrustServerCertificate=no",
            "connection_timeout": 5,
            "query_timeout": sql_query_timeout,
            "connection_retries": 0,
            "managed_identity_client_id": database_values["CLIENT_ID"],
        },
    }}
    DATABASE_CONNECTION_POOLING = False
    SCHEMA_TO_INSPECT = "'pisign'"
else:
    raise ImproperlyConfigured("PISIGN_DATABASE_ENGINE must be sqlite, postgresql, or mssql.")
ENTRA_TENANT_ID = os.environ.get("PISIGN_ENTRA_TENANT_ID", "")
ENTRA_SUBDOMAIN = os.environ.get("PISIGN_ENTRA_SUBDOMAIN", "")
ENTRA_CLIENT_ID = os.environ.get("PISIGN_ENTRA_CLIENT_ID", "")
ENTRA_CLIENT_SECRET = os.environ.get("PISIGN_ENTRA_CLIENT_SECRET", "")
ENTRA_REDIRECT_URI = os.environ.get(
    "PISIGN_ENTRA_REDIRECT_URI", "http://localhost:8000/accounts/callback/" if DEBUG else "",
)
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "pisign" / "static"]
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
    value for value in os.environ.get("PISIGN_CSRF_TRUSTED_ORIGINS", "").split(",") if value
]
# Enable only when a trusted proxy strips client-supplied forwarding headers.
if AZURE_CONTAINER_APPS or os.environ.get("PISIGN_TRUST_PROXY", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

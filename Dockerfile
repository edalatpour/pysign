# syntax=docker/dockerfile:1
FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl --fail --silent --show-error --location \
       https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb \
       --output /tmp/packages-microsoft-prod.deb \
    && dpkg -i /tmp/packages-microsoft-prod.deb \
    && rm /tmp/packages-microsoft-prod.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PISIGN_DATABASE_ENGINE=postgresql \
    PISIGN_POSTGRES_SSLROOTCERT=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app
RUN groupadd --system pisign && useradd --system --gid pisign --home-dir /app pisign

COPY pyproject.toml ./
COPY pisign ./pisign
COPY signage ./signage
COPY player ./player
COPY manage.py ./

RUN --mount=type=secret,id=pip_config,target=/etc/pip.conf,required=true \
    python -m pip install --no-cache-dir --only-binary=cryptography,psycopg-binary,pyodbc '.[server,production,azure-sql]' \
    && PISIGN_DATABASE_ENGINE=sqlite PISIGN_SECRET_KEY=build-only-static \
       python manage.py collectstatic --noinput

USER pisign
EXPOSE 8000
CMD ["gunicorn", "pisign.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-", "--error-logfile", "-", "--access-logformat", "%(h)s %(m)s %(U)s %(s)s %(L)s"]

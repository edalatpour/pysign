# syntax=docker/dockerfile:1
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYSIGN_DATABASE_ENGINE=postgresql \
    PYSIGN_POSTGRES_SSLROOTCERT=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app
RUN groupadd --system pysign && useradd --system --gid pysign --home-dir /app pysign

COPY pyproject.toml ./
COPY pysign ./pysign
COPY signage ./signage
COPY player ./player
COPY manage.py ./

RUN --mount=type=secret,id=pip_config,target=/etc/pip.conf,required=true \
    python -m pip install --no-cache-dir --only-binary=cryptography,psycopg-binary '.[server,production]' \
    && PYSIGN_DATABASE_ENGINE=sqlite PYSIGN_SECRET_KEY=build-only-static \
       python manage.py collectstatic --noinput

USER pysign
EXPOSE 8000
CMD ["gunicorn", "pysign.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-", "--error-logfile", "-", "--access-logformat", "%(h)s %(m)s %(U)s %(s)s %(L)s"]

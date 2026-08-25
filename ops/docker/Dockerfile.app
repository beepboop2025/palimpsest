# Palimpsest — full application image (API + Celery worker/beat collectors).
#
# This is the long-running service image, distinct from ops/docker/Dockerfile,
# which is the single-purpose, stdlib-only, throwaway sandbox for the weekly GFI
# reading. This image DOES install requirements.txt because the collectors,
# scheduler, and velocity worker need httpx, celery, sqlalchemy, etc. Hostile
# browser execution lives in Dockerfile.render-gateway; this privileged image
# deliberately contains no Chromium runtime.

FROM python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17 AS base

ARG PALIMPSEST_REVISION=unversioned
LABEL org.opencontainers.image.revision=$PALIMPSEST_REVISION

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# psycopg2-binary needs no build tools; keep the image lean. tini reaps zombies
# for the celery/uvicorn parents.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 palimpsest \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin palimpsest

WORKDIR /app

COPY requirements.lock .
RUN pip install --require-hashes --requirement requirements.lock

# Application code. Copy the packages the services import; leave out tests, docs,
# git history, and the ops/ deploy scaffolding.
COPY --chown=palimpsest:palimpsest api/          api/
COPY --chown=palimpsest:palimpsest core/         core/
COPY --chown=palimpsest:palimpsest evidence/     evidence/
COPY --chown=palimpsest:palimpsest collectors/   collectors/
COPY --chown=palimpsest:palimpsest processors/   processors/
COPY --chown=palimpsest:palimpsest storage/      storage/
COPY --chown=palimpsest:palimpsest censorwatch/  censorwatch/
COPY --chown=palimpsest:palimpsest config/       config/
COPY --chown=palimpsest:palimpsest scripts/      scripts/
# Shared DDTI file-to-reading adapter.  The node calls the same function the
# public workflow uses, keeping one output schema without invoking a shell.
COPY --chown=palimpsest:palimpsest inject_ddti.py inject_ddti.py

RUN mkdir -p /app/readings/state /app/data && chown -R palimpsest:palimpsest /app/readings /app/data

USER palimpsest

ENTRYPOINT ["/usr/bin/tini", "--"]
# Overridden per service in docker-compose.prod.yml (worker / beat / api).
CMD ["celery", "-A", "core.scheduler", "worker", "-c", "2"]

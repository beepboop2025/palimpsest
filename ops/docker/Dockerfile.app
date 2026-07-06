# Palimpsest — full application image (API + Celery worker/beat collectors).
#
# This is the long-running service image, distinct from ops/docker/Dockerfile,
# which is the single-purpose, stdlib-only, throwaway sandbox for the weekly GFI
# reading. This image DOES install requirements.txt because the collectors,
# scheduler, and (optional) velocity leg need httpx, celery, sqlalchemy, etc.
#
# Chromium for the CensorWatch velocity leg is heavy (~400MB) and only needed
# when CENSORWATCH_ENABLED is set, so it is gated behind WITH_BROWSER. Build the
# lean image by default; pass --build-arg WITH_BROWSER=true only if you run the
# velocity worker.

FROM python:3.12-slim AS base

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

COPY requirements.txt .
RUN pip install -r requirements.txt

# Optional: install the Chromium runtime for the velocity leg. --with-deps pulls
# the system libraries Chromium needs; it runs as root here (before USER) because
# it writes into /usr/lib and the browser cache under /root, which we relocate.
ARG WITH_BROWSER=false
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN if [ "$WITH_BROWSER" = "true" ]; then \
        playwright install --with-deps chromium \
        && chown -R palimpsest:palimpsest /opt/pw-browsers ; \
    fi

# Application code. Copy the packages the services import; leave out tests, docs,
# git history, and the ops/ deploy scaffolding.
COPY --chown=palimpsest:palimpsest api/          api/
COPY --chown=palimpsest:palimpsest core/         core/
COPY --chown=palimpsest:palimpsest collectors/   collectors/
COPY --chown=palimpsest:palimpsest processors/   processors/
COPY --chown=palimpsest:palimpsest storage/      storage/
COPY --chown=palimpsest:palimpsest censorwatch/  censorwatch/
COPY --chown=palimpsest:palimpsest config/       config/
COPY --chown=palimpsest:palimpsest scripts/      scripts/

RUN mkdir -p /app/readings/state /app/data && chown -R palimpsest:palimpsest /app/readings /app/data

USER palimpsest

ENTRYPOINT ["/usr/bin/tini", "--"]
# Overridden per service in docker-compose.prod.yml (worker / beat / api).
CMD ["celery", "-A", "core.scheduler", "worker", "-c", "2"]

# Palimpsest — hardened sandbox image for the OUTBOUND recurring reading.
#
# Scope: this image runs ONE thing — the recurring Generative Firewall Index
# reading (scripts/generative_firewall_reading.py), an outbound collector that
# reads a public model API and writes a reading into readings/. It is NOT a
# server; it publishes no ports and listens for nothing. The point of packaging
# it this way is blast-radius containment: if the collector is ever compromised
# by hostile data it fetches, it runs as a throwaway non-root process with no
# build tools, no writable root filesystem, and nothing of the host in reach.
#
# LINE-HELD: the analytical cores are standard-library only, so the recurring
# reading needs NO pip install at all (verified: it imports only stdlib +
# collectors.generative_firewall, which is itself stdlib). requirements.txt
# exists for the wider platform (API, Celery, Playwright velocity leg) — none of
# which run here — so we deliberately do not install it. See the optional block
# below if you ever extend this image to a collector that needs httpx et al.

FROM python:3.12-slim

# Fail loud, no .pyc litter, no stdout buffering (the reading logs progress).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root, least-privilege runtime identity. A high fixed UID/GID keeps the
# in-container user from colliding with any real host account if a bind is
# ever misconfigured.
RUN groupadd --gid 10001 palimpsest \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin palimpsest

WORKDIR /app

# --- OPTIONAL runtime deps -------------------------------------------------
# The recurring reading is stdlib-only, so this stays commented. If you extend
# this image to a collector that genuinely needs third-party packages, prefer a
# separate build stage so compilers/headers never reach the final image, e.g.:
#
#   FROM python:3.12-slim AS build
#   COPY requirements.txt .
#   RUN pip install --prefix=/install -r requirements.txt
#   FROM python:3.12-slim
#   COPY --from=build /install /usr/local
#
# Keep it minimal: install only what the collector you run actually imports.
# ---------------------------------------------------------------------------

# Copy ONLY what the recurring reading needs to run, owned by the non-root user.
# Everything else in the repo (api/, dashboards/, tests/, docs/, git history,
# secrets) is intentionally left out of the image.
COPY --chown=palimpsest:palimpsest scripts/generative_firewall_reading.py scripts/generative_firewall_reading.py
COPY --chown=palimpsest:palimpsest collectors/  collectors/
COPY --chown=palimpsest:palimpsest processors/  processors/
COPY --chown=palimpsest:palimpsest core/        core/
COPY --chown=palimpsest:palimpsest config/      config/

# Output directory. Under docker-compose this is shadowed by a read-write bind
# (readings/) so results persist, and readings/state is shadowed by a tmpfs so
# logs stay ephemeral. Creating it here lets the image also run standalone.
RUN mkdir -p /app/readings/state && chown -R palimpsest:palimpsest /app/readings

USER palimpsest

# Default: take one reading, then exit. This is a batch collector, not a daemon
# — the scheduler on the host (launchd) is what makes it recurring, and
# `restart: "no"` in compose keeps a crashed run from silently looping.
CMD ["python", "scripts/generative_firewall_reading.py"]

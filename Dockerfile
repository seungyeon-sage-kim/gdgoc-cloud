# One Dockerfile, four images. Each service is a "target" built on the same base:
#   docker build --target web              -t web .
#   docker build --target ticket-worker    -t ticket-worker .
#   docker build --target waitlist-promoter   -t waitlist-promoter .
#   docker build --target housekeeping -t housekeeping .
# The base layers (Python, dependencies, shared code) are built once and reused by all four.

# ---------- base: what every service needs ----------
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Cloud Run's disk is in memory and disappears with the instance; /tmp is writable
    DATA_DIR=/tmp/data

# Debian has fixed CVE-2026-76844 and CVE-2026-85091, but python:3.13-slim doesn't include
# the fix yet, and the image scan (Trivy) flags it. Remove this once the base image does.
RUN apt-get update && apt-get install --yes --no-install-recommends --only-upgrade zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first: this layer is reused until requirements.txt changes
COPY requirements.txt .
# pip is only needed to install. Removing it also removes its bundled copies of
# urllib3, msgpack and setuptools, which the image scan (Trivy) flags.
RUN pip install --no-cache-dir -r requirements.txt && pip uninstall --yes pip

COPY shared/ shared/

# Run as a regular user, not root (and give it the data folder)
RUN useradd --uid 10001 --no-create-home app && mkdir -p /tmp/data && chown app /tmp/data
USER 10001

# ---------- web: the public service ----------
FROM base AS web
COPY web/ web/
# Cloud Run tells the container which port to listen on through $PORT (8080 by default).
# JSON form + exec: uvicorn becomes PID 1 and gets Cloud Run's shutdown signal directly.
CMD ["sh", "-c", "exec uvicorn web.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips \"*\""]

# ---------- ticket-worker: receives Pub/Sub push messages ----------
FROM base AS ticket-worker
COPY ticket_worker/ ticket_worker/
CMD ["sh", "-c", "exec uvicorn ticket_worker.main:app --host 0.0.0.0 --port ${PORT:-8080}"]

# ---------- waitlist-promoter: receives Firestore changes from Eventarc ----------
FROM base AS waitlist-promoter
COPY waitlist_promoter/ waitlist_promoter/
CMD ["sh", "-c", "exec uvicorn waitlist_promoter.main:app --host 0.0.0.0 --port ${PORT:-8080}"]

# ---------- housekeeping: a Cloud Run job, started every 5 minutes, runs and exits ----------
FROM base AS housekeeping
# ENTRYPOINT, not CMD: arguments such as --check or --force-release are passed to the job
# (gcloud run jobs execute --args, docker compose run)
COPY jobs/ jobs/
ENTRYPOINT ["python", "-m", "jobs.housekeeping"]

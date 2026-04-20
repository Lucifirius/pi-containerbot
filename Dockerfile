# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install deps into an isolated prefix so we can copy them cleanly
COPY requirements.txt .
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="EVE Fuel Monitor"
LABEL org.opencontainers.image.description="Monitors corp hangar fuel blocks and posts to Discord"

# Non-root user for security
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application
COPY fuel_monitor.py .

# /data is the mount point for config.yaml and tokens.json
RUN mkdir /data && chown appuser:appuser /data

USER appuser

# Environment defaults (override in docker-compose or with -e flags)
ENV CONFIG_PATH=/data/config.yaml \
    TOKEN_PATH=/data/tokens.json  \
    STATE_PATH=/data/state.json   \
    PYTHONUNBUFFERED=1

# Default: watch mode, interval supplied via CMD or WATCH_INTERVAL env
ENV WATCH_INTERVAL=60

# Entrypoint runs the monitor in watch mode by default.
# The auth and discord-test services override CMD in docker-compose.yml.
CMD python fuel_monitor.py --watch ${WATCH_INTERVAL}

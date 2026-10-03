# syntax=docker/dockerfile:1

FROM node:22-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

# Chromium (render) and faster-whisper (transcribe) are left out on purpose: too large. EXTRAS=pdf drops yt-dlp.
ARG EXTRAS=social,pdf

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/data \
    XDG_CACHE_HOME=/data/.cache \
    DIGEST_FRONTEND_DIST=/app/frontend/dist \
    DIGEST_CONFIG=/config/config.toml

WORKDIR /app
COPY pyproject.toml ./
COPY digest/ digest/
RUN pip install ".[${EXTRAS}]"

COPY --from=frontend /build/frontend/dist frontend/dist
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
RUN chmod 0755 /app/docker/entrypoint.sh \
    && useradd --system --uid 10001 --no-create-home --home-dir /data digest \
    && mkdir -p /data /config \
    && chown digest /data

USER digest
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3)"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["serve"]

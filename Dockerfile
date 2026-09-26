# syntax=docker/dockerfile:1

# Keep Python dependencies exactly at uv.lock's frozen resolution.
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libchromaprint1 libmp3lame0 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 yoto \
    && useradd --system --uid 10001 --gid yoto --create-home --home-dir /home/yoto yoto \
    && install -d --owner=yoto --group=yoto --mode=0700 \
        /var/lib/yoto-mcp/session /var/lib/yoto-mcp/jobs /var/lib/yoto-mcp/uploads

ENV PATH="/opt/venv/bin:${PATH}" \
    HOME=/home/yoto \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    YOTO_SESSION_FILE=/var/lib/yoto-mcp/session/oauth-session.json \
    YOTO_JOB_ROOT=/var/lib/yoto-mcp/jobs \
    YOTO_UPLOAD_ROOT=/var/lib/yoto-mcp/uploads
WORKDIR /app
COPY --from=builder --chown=10001:10001 /opt/venv /opt/venv
COPY --from=builder --chown=10001:10001 /app/src /app/src

# Fail the build if the selected Debian ffmpeg lacks features required by the
# existing media pipeline, or if the lockfile's pinned yt-dlp is not executable.
RUN set -eu; \
    test -x /usr/bin/ffmpeg || { echo 'Missing /usr/bin/ffmpeg'; exit 1; }; \
    test -x /usr/bin/ffprobe || { echo 'Missing /usr/bin/ffprobe'; exit 1; }; \
    /usr/bin/ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'libmp3lame' \
      || { echo 'FFmpeg lacks libmp3lame encoder'; exit 1; }; \
    /usr/bin/ffmpeg -hide_banner -muxers 2>/dev/null | grep -q 'chromaprint' \
      || { echo 'FFmpeg lacks Chromaprint muxer'; exit 1; }; \
    /opt/venv/bin/python -c "import importlib.metadata as m; assert m.version('yt-dlp') == '2026.8.19'"; \
    /opt/venv/bin/yt-dlp --version >/dev/null

ARG BUILD_COMMIT=unknown
ARG BUILD_TIME=unknown
ARG BUILD_TAG=unknown
LABEL org.opencontainers.image.source="https://github.com/RealBeepMcJeep/yoto-mcp" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.version="${BUILD_TAG}"
ENV YOTO_BUILD_COMMIT=${BUILD_COMMIT} \
    YOTO_BUILD_TIME=${BUILD_TIME} \
    YOTO_BUILD_TAG=${BUILD_TAG}

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).read()"]
CMD ["yoto-mcp", "serve", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]

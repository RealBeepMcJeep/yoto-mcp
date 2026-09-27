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

# CPU-only whisper.cpp for transcribe_lyrics. GGML_NATIVE=OFF + CPU_ALL_VARIANTS
# builds every x86 variant and picks one at runtime, so an image built on a CI
# runner cannot crash with illegal instructions on an older NAS CPU.
FROM python:3.12-slim AS whisper
ARG WHISPER_CPP_VERSION=v1.9.4
ARG WHISPER_MODEL=base
ARG WHISPER_MODEL_SHA256=60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${WHISPER_CPP_VERSION}" https://github.com/ggml-org/whisper.cpp.git /src \
    && cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON \
        -DGGML_CPU_ALL_VARIANTS=ON -DWHISPER_BUILD_TESTS=OFF '-DCMAKE_BUILD_RPATH=$ORIGIN' \
    && cmake --build /build -j"$(nproc)" --target whisper-cli \
    && install -d /opt/whisper \
    && cp -a /build/bin/whisper-cli /build/bin/*.so* /opt/whisper/ \
    && curl -fsSL -o "/opt/whisper/ggml-${WHISPER_MODEL}.bin" \
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${WHISPER_MODEL}.bin" \
    && echo "${WHISPER_MODEL_SHA256}  /opt/whisper/ggml-${WHISPER_MODEL}.bin" | sha256sum -c -

FROM python:3.12-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libchromaprint1 libmp3lame0 \
        libgomp1 libstdc++6 \
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
    YOTO_UPLOAD_ROOT=/var/lib/yoto-mcp/uploads \
    YOTO_WHISPER_CLI=/opt/whisper/whisper-cli \
    YOTO_WHISPER_MODEL=/opt/whisper/ggml-base.bin
WORKDIR /app
COPY --from=builder --chown=10001:10001 /opt/venv /opt/venv
COPY --from=builder --chown=10001:10001 /app/src /app/src
COPY --from=whisper /opt/whisper /opt/whisper

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
    /opt/venv/bin/yt-dlp --version >/dev/null; \
    /opt/whisper/whisper-cli -h >/dev/null 2>&1 || { echo 'whisper-cli does not run'; exit 1; }; \
    /usr/bin/ffmpeg -v error -f lavfi -i sine=frequency=440:duration=2 -ar 16000 -ac 1 /tmp/tone.wav; \
    /opt/whisper/whisper-cli -m /opt/whisper/ggml-base.bin -f /tmp/tone.wav -t 1 -np >/dev/null 2>&1 \
      || { echo 'whisper-cli cannot load the bundled model'; exit 1; }; \
    rm /tmp/tone.wav

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

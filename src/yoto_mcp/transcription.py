"""CPU lyric transcription through a bundled whisper.cpp CLI, with a private disk cache.

A transcript is an independent second opinion on sung words, not ground truth:
vocals, effects and backing tracks cause omissions and hallucinations.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .media import resolve_audio
from .youtube_source import YouTubeSourceError, _completed, _media_binary

MAX_AUDIO_SECONDS = 3600
MAX_TRANSCRIPT_BYTES = 1024 * 1024
DECODE_TIMEOUT = 120


class TranscriptionError(RuntimeError):
    """Sanitized transcription failure; never includes subprocess output."""


def _absolute_file(env: Mapping[str, str], key: str, default: str, label: str) -> Path:
    configured = env.get(key, default)
    path = Path(configured)
    if not path.is_absolute() or not path.is_file():
        raise TranscriptionError(f"{label} is not installed; set {key} to an absolute path")
    return path


def _cache_root(env: Mapping[str, str]) -> Path:
    if env.get("YOTO_TRANSCRIPT_CACHE"):
        return Path(env["YOTO_TRANSCRIPT_CACHE"])
    base = env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "yoto-mcp" / "transcripts"


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        value = int(env.get(key, default))
    except ValueError:
        raise TranscriptionError(f"{key} must be a positive integer") from None
    if value < 1:
        raise TranscriptionError(f"{key} must be a positive integer")
    return value


def _write_private_json(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tx-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def transcribe_audio(
    upload_root: Path,
    file_path: str,
    *,
    refresh: bool = False,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Transcribe one audio file under upload_root; cached by audio bytes + model."""
    env = os.environ if env is None else env
    source = resolve_audio(upload_root, file_path)
    cli = _absolute_file(env, "YOTO_WHISPER_CLI", "/opt/whisper/whisper-cli", "whisper-cli")
    model = _absolute_file(env, "YOTO_WHISPER_MODEL", "/opt/whisper/ggml-base.bin", "Whisper model")
    threads = _positive_int(env, "YOTO_WHISPER_THREADS", min(4, os.cpu_count() or 1))
    timeout = _positive_int(env, "YOTO_WHISPER_TIMEOUT", 900)

    audio_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    # A model is identified by name + size: cheap, and changes whenever the file is swapped.
    model_id = f"{model.stem}-{model.stat().st_size}"
    cache_file = _cache_root(env) / f"{audio_sha256}-{model_id}.json"
    if not refresh and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and isinstance(cached.get("text"), str):
                return {**cached, "cache_hit": True}
        except (OSError, ValueError):
            pass  # Corrupt cache entry: transcribe again and overwrite it.

    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="yoto-tx-") as work:
            wav = Path(work) / "audio.wav"
            _completed(runner, [
                _media_binary("ffmpeg"), "-nostdin", "-v", "error", "-i", str(source),
                "-t", str(MAX_AUDIO_SECONDS + 1), "-ar", "16000", "-ac", "1",
                "-c:a", "pcm_s16le", str(wav),
            ], timeout=DECODE_TIMEOUT)
            duration = max(0, wav.stat().st_size - 44) / 32000
            if duration > MAX_AUDIO_SECONDS:
                raise TranscriptionError("Audio is longer than one hour")
            if duration < 1:
                raise TranscriptionError("Audio is shorter than one second")
            # -sns suppresses non-speech tokens: without it Whisper labels most sung
            # vocals "[Music]" and emits almost no words (measured 0% -> 68-91% recall).
            result = _completed(runner, [
                str(cli), "-m", str(model), "-f", str(wav), "-t", str(threads),
                "-l", "auto", "-nt", "-np", "-sns",
            ], timeout=timeout, output_limit=MAX_TRANSCRIPT_BYTES)
    except YouTubeSourceError:
        raise TranscriptionError("Audio decode or transcription failed or timed out") from None
    except OSError:
        raise TranscriptionError("Audio decode produced no readable output") from None

    text = "\n".join(line.strip() for line in result.stdout.splitlines() if line.strip())
    record = {
        "status": "complete",
        "text": text,
        "model": model_id,
        "audio_sha256": audio_sha256,
        "audio_seconds": round(duration, 1),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "threads": threads,
    }
    _write_private_json(cache_file, record)
    return {**record, "cache_hit": False}

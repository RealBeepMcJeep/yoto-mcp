"""Single-use MP3 upload links for the Streamable HTTP deployment.

An authenticated MCP call (``create_mp3_upload``) mints a link; the agent then
sends the raw MP3 bytes to it with a plain HTTP PUT (e.g. ``curl -T``). The
random secret in the link is the only credential the PUT needs, so the server's
bearer token never has to appear in a shell command or transcript. A link works
once, expires after ``LINK_TTL_SECONDS``, and only stores a verified MP3 under
``YOTO_UPLOAD_ROOT/inbox``; adding it to a playlist is still ``add_mp3``.
"""

from __future__ import annotations

import json
import math
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .media import MAX_FILE_BYTES, sanitize_filename_stem
from .metadata import MetadataLookupError, format_track_title, read_mp3_tags
from .youtube_source import _media_binary, _run_capture

LINK_TTL_SECONDS = 15 * 60
INBOX_RETENTION_SECONDS = 24 * 60 * 60
INBOX = "inbox"
_PROBE_TIMEOUT_SECONDS = 60


class UploadRejected(ValueError):
    """The uploaded bytes are not an acceptable MP3; ``status`` is the HTTP reply code."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def probe_mp3(path: Path) -> float:
    """Return the duration of a real MP3 via the vetted ffprobe, or raise UploadRejected."""
    try:
        raw = _run_capture(
            subprocess.run,
            [_media_binary("ffprobe"), "-v", "error", "-show_entries",
             "format=duration:stream=codec_name", "-of", "json", str(path)],
            timeout=_PROBE_TIMEOUT_SECONDS, output_limit=64 * 1024,
        )
        probe = json.loads(raw)
        codecs = [stream.get("codec_name") for stream in probe["streams"]]
        duration = float(probe["format"]["duration"])
    except Exception:  # noqa: BLE001 - any probe failure means "not a readable MP3"
        raise UploadRejected("File is not a readable MP3") from None
    if "mp3" not in codecs or not math.isfinite(duration) or duration <= 0:
        raise UploadRejected("File is not a readable MP3")
    return duration


@dataclass
class _Link:
    expires_at: float
    stem: str


class UploadLinks:
    """In-memory single-use links; a server restart simply invalidates them.

    ponytail: one-process store, fine for the single uvicorn worker this server runs;
    persist links in the job root if it ever runs several workers.
    """

    def __init__(
        self, upload_root: Path, *, clock: Callable[[], float] = time.time,
        probe: Callable[[Path], float] | None = None,
    ) -> None:
        self.upload_root = Path(upload_root)
        self.clock = clock
        self.probe = probe if probe is not None else probe_mp3
        self._links: dict[str, _Link] = {}
        self._lock = threading.Lock()

    def create(self, filename: str | None = None) -> str:
        stem = sanitize_filename_stem(Path(filename).stem if filename else "upload")
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._links = {key: link for key, link in self._links.items() if link.expires_at > now}
            self._links[token] = _Link(now + LINK_TTL_SECONDS, stem)
        self._sweep_inbox(now)
        return token

    def _claim(self, token: str) -> _Link | None:
        with self._lock:
            link = self._links.pop(token, None)
        return link if link is not None and link.expires_at > self.clock() else None

    def _sweep_inbox(self, now: float) -> None:
        inbox = self.upload_root / INBOX
        if not inbox.is_dir():
            return
        for entry in inbox.iterdir():
            try:
                if now - entry.lstat().st_mtime <= INBOX_RETENTION_SECONDS:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except OSError:
                pass

    async def receive(self, request: Request) -> Response:
        link = self._claim(request.path_params.get("token", ""))
        if link is None:
            return _error(404, "Upload link is invalid, expired, or already used; call create_mp3_upload for a new one")
        if request.headers.get("content-type", "").startswith("multipart/"):
            return _error(415, "Send the raw MP3 bytes as the request body (curl -T file.mp3 URL), not a form upload")
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_FILE_BYTES:
            return _error(413, "MP3 exceeds the 100 MiB limit")

        inbox = self.upload_root / INBOX
        inbox.mkdir(mode=0o700, exist_ok=True)
        folder = inbox / secrets.token_hex(8)
        folder.mkdir(mode=0o700)
        target = folder / f"{link.stem}.mp3"
        try:
            size = 0
            with target.open("xb") as handle:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise UploadRejected("MP3 exceeds the 100 MiB limit", 413)
                    handle.write(chunk)
            if size < 128:
                raise UploadRejected("Upload is empty or too small to be an MP3")
            target.chmod(0o600)
            duration = await run_in_threadpool(self.probe, target)
            tags = await run_in_threadpool(_tags, target)
        except UploadRejected as exc:
            shutil.rmtree(folder, ignore_errors=True)
            return _error(exc.status, str(exc))
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        file_path = target.relative_to(self.upload_root).as_posix()
        suggested = (format_track_title(tags["artist"], tags["title"])
                     if tags.get("artist") and tags.get("title") else tags.get("title") or link.stem)
        return JSONResponse({
            "file_path": file_path,
            "size_bytes": size,
            "duration_seconds": round(duration, 3),
            "tags": tags,
            "suggested_title": suggested,
            "expires_in_hours": INBOX_RETENTION_SECONDS // 3600,
            "next_step": (
                f"Call add_mp3(card_id=..., chapter_key='new', file_path='{file_path}', title=...) "
                "with dry_run=true to preview, then dry_run=false to add it."
            ),
        }, status_code=201)


def _tags(path: Path) -> dict[str, Any]:
    try:
        return read_mp3_tags(path)
    except MetadataLookupError:
        return {}


def _error(status: int, message: str) -> Response:
    return JSONResponse({"error": message}, status_code=status)


def upload_url(request: Request, token: str) -> str:
    """Build the link on the same scheme/host the agent used to reach /mcp (Host is allowlisted)."""
    return f"{str(request.base_url).rstrip('/')}/uploads/{token}"

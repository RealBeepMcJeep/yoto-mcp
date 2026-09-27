"""Validate bounded local MP3 inputs and use Yoto's URL-safe SHA-256 encoding."""

from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
from pathlib import Path

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_IMAGE_SIGNATURES: dict[bytes, str] = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}


def resolve_image(root: Path, value: str) -> tuple[Path, str]:
    """Resolve a local icon source image and return it with its sniffed MIME type."""
    if not value or not value.strip():
        raise ValueError("An image path is required")
    try:
        safe_root = root.resolve(strict=True)
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = safe_root / candidate
        safe_path = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Image path does not exist") from exc
    if not safe_path.is_relative_to(safe_root) or not safe_path.is_file():
        raise ValueError("Image path must be a file inside YOTO_UPLOAD_ROOT")
    size = safe_path.stat().st_size
    if size < 8 or size > MAX_IMAGE_BYTES:
        raise ValueError("Image file is empty, too small, or exceeds 10 MiB")
    with safe_path.open("rb") as stream:
        header = stream.read(8)
    mime = next((kind for sig, kind in _IMAGE_SIGNATURES.items() if header.startswith(sig)), None)
    if mime is None:
        raise ValueError("File is not a recognized PNG, JPEG, or GIF image")
    return safe_path, mime


def resolve_audio(root: Path, value: str) -> Path:
    """Resolve any local audio file under root; FFmpeg decoding validates the format."""
    if not value or not value.strip():
        raise ValueError("An audio path is required")
    try:
        safe_root = root.resolve(strict=True)
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = safe_root / candidate
        safe_path = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Audio path does not exist") from exc
    if not safe_path.is_relative_to(safe_root) or not safe_path.is_file():
        raise ValueError("Audio path must be a file inside YOTO_UPLOAD_ROOT")
    size = safe_path.stat().st_size
    if size < 128 or size > MAX_FILE_BYTES:
        raise ValueError("Audio file is empty, too small, or exceeds 100 MiB")
    return safe_path


def ascii_header_filename(name: str) -> str:
    """Return an ASCII-safe filename for HTTP header values.

    httpx encodes header values as ASCII, so a filename carrying diacritics or
    non-Latin scripts would raise UnicodeEncodeError before the request leaves.
    The folded text is only S3 metadata; the object key comes from the presigned
    URL and the local filename is untouched.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").strip()
    if not folded or folded.startswith("."):
        # Nothing but an extension remains (e.g. an all non-Latin name) - use a placeholder.
        folded = "upload.mp3"
    return folded


def resolve_mp3(root: Path, value: str) -> Path:
    if not value or not value.strip():
        raise ValueError("An MP3 path is required")
    try:
        safe_root = root.resolve(strict=True)
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = safe_root / candidate
        safe_path = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("MP3 path does not exist") from exc
    if not safe_path.is_relative_to(safe_root) or not safe_path.is_file():
        raise ValueError("MP3 path must be a file inside YOTO_UPLOAD_ROOT")
    if safe_path.suffix.lower() != ".mp3":
        raise ValueError("Only MP3 files are supported")
    size = safe_path.stat().st_size
    if size < 128 or size > MAX_FILE_BYTES:
        raise ValueError("MP3 file is empty, too small, or exceeds 100 MiB")
    with safe_path.open("rb") as stream:
        header = stream.read(3)
    if header != b"ID3" and not (header[0] == 0xFF and header[1] & 0xE0 == 0xE0):
        raise ValueError("File does not have an MP3 header")
    return safe_path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")


def sanitize_filename_stem(value: str) -> str:
    """Collapse a track title into a safe local filename stem (no extension)."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:150] or "track"

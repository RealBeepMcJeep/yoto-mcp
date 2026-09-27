"""LRCLIB lyric-evidence lookup.

Text-only provider lookup for a family caller to review; no CPU transcription,
no Yoto account access, no persistent cache. Scope and rights gates are
recorded in plans/lyrics-evidence-and-content-review.md. This module is
identity + provider lookup only (implementation-sequence slices 2-3 of that
plan); the audio second-opinion/cache/review slices are separate follow-ups.
"""

from __future__ import annotations

from typing import Any

import httpx

LRCLIB_BASE_URL = "https://lrclib.net/api"
USER_AGENT = "yoto-mcp/0.1 (+https://github.com/RealBeepMcJeep/yoto-mcp)"
_MAX_STRING_LENGTH = 200


class LyricLookupError(RuntimeError):
    """Sanitized lyric-provider error; never includes raw response bodies."""


def _valid_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= _MAX_STRING_LENGTH
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


def lookup_lyric_evidence(
    artist: str,
    title: str,
    *,
    album: str | None = None,
    duration_seconds: int | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Query LRCLIB for one recording's lyric candidate (GET /api/get).

    Returns a dict with a ``status`` of ``matched|instrumental|no_match|
    provider_unavailable``. Only a transport/format failure raises
    LyricLookupError; a normal miss is a returned status, not an exception.
    """
    if not _valid_text(artist) or not _valid_text(title):
        raise ValueError("artist and title must be 1-200 printable characters")
    if album is not None and not _valid_text(album):
        raise ValueError("album must be 1-200 printable characters")
    if duration_seconds is not None and (
        not isinstance(duration_seconds, int)
        or isinstance(duration_seconds, bool)
        or not (1 <= duration_seconds <= 3600)
    ):
        raise ValueError("duration_seconds must be an integer between 1 and 3600")

    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration_seconds is not None:
        params["duration"] = duration_seconds

    try:
        with httpx.Client(transport=transport, timeout=10.0) as client:
            response = client.get(
                f"{LRCLIB_BASE_URL}/get", params=params, headers={"User-Agent": USER_AGENT},
            )
    except httpx.HTTPError:
        raise LyricLookupError("LRCLIB request failed") from None

    if response.status_code == 404:
        return {"status": "no_match"}
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        return {
            "status": "provider_unavailable",
            "retry_after": int(retry_after) if isinstance(retry_after, str) and retry_after.isdigit() else None,
        }
    if response.status_code >= 500:
        return {"status": "provider_unavailable"}
    if response.status_code != 200:
        raise LyricLookupError(f"LRCLIB returned HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError:
        raise LyricLookupError("LRCLIB returned an invalid response") from None
    if not isinstance(data, dict):
        raise LyricLookupError("LRCLIB returned an invalid response")

    if data.get("instrumental") is True:
        return {
            "status": "instrumental",
            "source_id": data.get("id"),
            "source_url": "https://lrclib.net/",
        }

    plain = data.get("plainLyrics")
    synced = data.get("syncedLyrics")
    if not isinstance(plain, str) and not isinstance(synced, str):
        return {"status": "no_match"}

    return {
        "status": "matched",
        "name": "lrclib",
        "source_id": data.get("id"),
        "source_url": "https://lrclib.net/",
        "track_name": data.get("trackName") if isinstance(data.get("trackName"), str) else None,
        "artist_name": data.get("artistName") if isinstance(data.get("artistName"), str) else None,
        "album_name": data.get("albumName") if isinstance(data.get("albumName"), str) else None,
        "duration": data.get("duration") if isinstance(data.get("duration"), (int, float)) else None,
        "plain_lyrics": plain if isinstance(plain, str) else None,
        "synced_lyrics": synced if isinstance(synced, str) else None,
    }

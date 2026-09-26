"""Bounded, side-effect-free ID3 and MusicBrainz metadata helpers."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import httpx

ID3_MAX_TAG_BYTES = 16 * 1024 * 1024
ID3_MAX_TEXT_FRAME_BYTES = 64 * 1024
ID3_MAX_FRAMES = 1000

_TAG_FRAME_NAMES = {"TPE1": "artist", "TIT2": "title", "TALB": "album"}
_EMPTY_TAGS = {"artist": "", "title": "", "album": ""}
MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/recording/"
MUSICBRAINZ_USER_AGENT = "yoto-mcp/0.1.0 (local metadata lookup)"
MUSICBRAINZ_MAX_QUERY_CHARS = 256
MUSICBRAINZ_MAX_RESPONSE_BYTES = 1024 * 1024
MUSICBRAINZ_MAX_CANDIDATES = 10


class MetadataLookupError(ValueError):
    """Sanitized input or MusicBrainz response error."""


def _syncsafe_value(data: bytes) -> int:
    if len(data) != 4 or any(byte & 0x80 for byte in data):
        raise MetadataLookupError("Malformed ID3 tag")
    return (data[0] << 21) | (data[1] << 14) | (data[2] << 7) | data[3]


def _decode_unsynchronised(data: bytes) -> bytes:
    result = bytearray()
    index = 0
    while index < len(data):
        byte = data[index]
        result.append(byte)
        index += 1
        if byte == 0xFF and index < len(data) and data[index] == 0:
            index += 1
    return bytes(result)


def _take_v23_unsynchronised(data: bytes, start: int, decoded_size: int) -> tuple[bytes, int]:
    decoded = bytearray()
    index = start
    while len(decoded) < decoded_size and index < len(data):
        byte = data[index]
        decoded.append(byte)
        index += 1
        if byte == 0xFF and index < len(data) and data[index] == 0:
            index += 1
    if len(decoded) != decoded_size:
        raise MetadataLookupError("Malformed ID3 tag")
    return bytes(decoded), index


def _skip_extended_header(body: bytes, version: int) -> int:
    if version == 3:
        if len(body) < 4:
            raise MetadataLookupError("Malformed ID3 tag")
        size = int.from_bytes(body[:4], "big")
        total_size = size + 4
        if size not in (6, 10) or total_size > len(body):
            raise MetadataLookupError("Malformed ID3 tag")
        flags = int.from_bytes(body[4:6], "big")
        if flags not in (0, 0x8000) or (flags == 0 and size != 6) or (flags == 0x8000 and size != 10):
            raise MetadataLookupError("Malformed ID3 tag")
        return total_size

    if len(body) < 6:
        raise MetadataLookupError("Malformed ID3 tag")
    total_size = _syncsafe_value(body[:4])
    if total_size < 6 or total_size > len(body) or body[4] != 1:
        raise MetadataLookupError("Malformed ID3 tag")
    flags = body[5]
    if flags & ~0x70:
        raise MetadataLookupError("Malformed ID3 tag")
    index = 6
    for bit, expected_length in ((0x40, 0), (0x20, 5), (0x10, 1)):
        if flags & bit:
            if index >= total_size or body[index] != expected_length:
                raise MetadataLookupError("Malformed ID3 tag")
            index += 1 + expected_length
    if index != total_size:
        raise MetadataLookupError("Malformed ID3 tag")
    return total_size


def _decode_text_frame(payload: bytes, version: int) -> str:
    if not payload or len(payload) > ID3_MAX_TEXT_FRAME_BYTES:
        raise MetadataLookupError("Malformed ID3 text frame")
    encoding = payload[0]
    codecs = {0: "latin-1", 1: "utf-16"}
    if version == 4:
        codecs.update({2: "utf-16-be", 3: "utf-8"})
    codec = codecs.get(encoding)
    if codec is None:
        raise MetadataLookupError("Malformed ID3 text frame")
    raw = payload[1:]
    if encoding == 1 and (len(raw) < 2 or raw[:2] not in (b"\xff\xfe", b"\xfe\xff")):
        raise MetadataLookupError("Malformed ID3 text frame")
    if encoding in (1, 2) and len(raw) % 2:
        raise MetadataLookupError("Malformed ID3 text frame")
    try:
        return raw.decode(codec, errors="strict").split("\x00", 1)[0].strip()
    except UnicodeError:
        raise MetadataLookupError("Malformed ID3 text frame") from None


def _parse_id3_tag(header: bytes, stream: Any) -> dict[str, str]:
    version = header[3]
    flags = header[5]
    allowed_flags = 0xE0 if version == 3 else 0xF0
    if version not in (3, 4) or flags & ~allowed_flags:
        raise MetadataLookupError("Malformed ID3 tag")
    tag_size = _syncsafe_value(header[6:10])
    if tag_size > ID3_MAX_TAG_BYTES:
        raise MetadataLookupError("ID3 tag exceeds size limit")
    body = stream.read(tag_size)
    if len(body) != tag_size:
        raise MetadataLookupError("Malformed ID3 tag")

    if version == 4 and flags & 0x10:
        if len(body) < 10:
            raise MetadataLookupError("Malformed ID3 tag")
        footer = body[-10:]
        if footer[:3] != b"3DI" or footer[3:6] != header[3:6] or footer[6:10] != header[6:10]:
            raise MetadataLookupError("Malformed ID3 tag")
        body = body[:-10]

    offset = _skip_extended_header(body, version) if flags & 0x40 else 0
    unsynchronised = bool(flags & 0x80)
    result = dict(_EMPTY_TAGS)
    seen: set[str] = set()
    frame_count = 0

    while offset < len(body):
        if body[offset] == 0:
            if any(body[offset:]):
                raise MetadataLookupError("Malformed ID3 padding")
            break
        if len(body) - offset < 10:
            raise MetadataLookupError("Malformed ID3 frame")
        frame_id_bytes = body[offset : offset + 4]
        if any(not (65 <= byte <= 90 or 48 <= byte <= 57) for byte in frame_id_bytes):
            raise MetadataLookupError("Malformed ID3 frame")
        frame_id = frame_id_bytes.decode("ascii")
        size_data = body[offset + 4 : offset + 8]
        frame_size = _syncsafe_value(size_data) if version == 4 else int.from_bytes(size_data, "big")
        status_flags, format_flags = body[offset + 8 : offset + 10]
        if version == 3:
            if status_flags & ~0xE0 or format_flags & ~0xE0:
                raise MetadataLookupError("Malformed ID3 frame")
        elif status_flags & ~0x70 or format_flags & ~0x4F:
            raise MetadataLookupError("Malformed ID3 frame")
        frame_start = offset + 10
        if frame_size > len(body) - frame_start:
            raise MetadataLookupError("Malformed ID3 frame")
        if version == 3 and unsynchronised:
            payload, offset = _take_v23_unsynchronised(body, frame_start, frame_size)
        else:
            payload = body[frame_start : frame_start + frame_size]
            offset = frame_start + frame_size
        if frame_count >= ID3_MAX_FRAMES:
            raise MetadataLookupError("ID3 tag exceeds frame limit")
        frame_count += 1
        field = _TAG_FRAME_NAMES.get(frame_id)
        if field is not None:
            if field in seen or format_flags:
                raise MetadataLookupError("Malformed ID3 text frame")
            if frame_size > ID3_MAX_TEXT_FRAME_BYTES:
                raise MetadataLookupError("ID3 text frame exceeds size limit")
            if unsynchronised and version == 4:
                payload = _decode_unsynchronised(payload)
            result[field] = _decode_text_frame(payload, version)
            seen.add(field)
        elif format_flags & (0xE0 if version == 3 else 0x4C):
            raise MetadataLookupError("Unsupported ID3 frame encoding")

    return result


def read_mp3_tags(path: Path) -> dict[str, str]:
    """Read artist, title, and album from bounded ID3v2.3/v2.4 tags.

    MP3s without an ID3v2 tag return empty fields. Malformed or oversized tags
    are rejected rather than returning potentially misleading partial metadata.
    """
    try:
        with Path(path).open("rb") as stream:
            header = stream.read(10)
            if header[:3] != b"ID3":
                return dict(_EMPTY_TAGS)
            if len(header) != 10:
                raise MetadataLookupError("Malformed ID3 tag")
            return _parse_id3_tag(header, stream)
    except MetadataLookupError:
        raise
    except (OSError, TypeError, ValueError):
        raise MetadataLookupError("Unable to read MP3 metadata") from None


def format_track_title(artist: str | None, title: str | None) -> str:
    """Format a Yoto track label without appending a filename extension."""
    clean_artist = artist.strip() if isinstance(artist, str) else ""
    clean_title = title.strip() if isinstance(title, str) else ""
    if clean_artist and clean_title:
        return f"{clean_artist} — {clean_title}"
    return clean_title or clean_artist or "Untitled"


def _artist_credit(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            artist = item.get("artist")
            name = artist.get("name") if isinstance(artist, dict) else None
            if isinstance(name, str):
                parts.append(name)
            joinphrase = item.get("joinphrase")
            if isinstance(joinphrase, str):
                parts.append(joinphrase)
    return "".join(parts).strip()


def _read_bounded_response(response: httpx.Response) -> bytes:
    length = response.headers.get("content-length")
    if length is not None:
        try:
            too_large = int(length) > MUSICBRAINZ_MAX_RESPONSE_BYTES
        except ValueError:
            raise MetadataLookupError("Invalid MusicBrainz response") from None
        if too_large:
            raise MetadataLookupError("MusicBrainz response exceeds size limit")
    content = bytearray()
    for chunk in response.iter_bytes():
        if len(content) + len(chunk) > MUSICBRAINZ_MAX_RESPONSE_BYTES:
            raise MetadataLookupError("MusicBrainz response exceeds size limit")
        content.extend(chunk)
    return bytes(content)


def lookup_recordings(query: str, transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    """Return up to ten public MusicBrainz candidates without auto-selection or writes."""
    if not isinstance(query, str) or not query.strip() or len(query) > MUSICBRAINZ_MAX_QUERY_CHARS:
        raise MetadataLookupError("Query must be non-empty and no longer than 256 characters")

    try:
        with httpx.Client(
            transport=transport,
            timeout=10.0,
            headers={"Accept": "application/json", "User-Agent": MUSICBRAINZ_USER_AGENT},
            follow_redirects=False,
        ) as client, client.stream(
            "GET",
            MUSICBRAINZ_URL,
            params={"query": query.strip(), "fmt": "json", "limit": str(MUSICBRAINZ_MAX_CANDIDATES)},
        ) as response:
            response.raise_for_status()
            raw = _read_bounded_response(response)
        data = json.loads(raw)
        recordings = data.get("recordings") if isinstance(data, dict) else None
        if not isinstance(recordings, list):
            raise MetadataLookupError("Invalid MusicBrainz response")
    except MetadataLookupError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, RecursionError):
        raise MetadataLookupError("MusicBrainz lookup failed") from None

    candidates: list[dict[str, Any]] = []
    for recording in recordings:
        if not isinstance(recording, dict):
            continue
        recording_id = recording.get("id")
        title = recording.get("title")
        score = recording.get("score")
        if (
            not isinstance(recording_id, str)
            or not recording_id
            or not isinstance(title, str)
            or not title
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
            or (isinstance(score, float) and not math.isfinite(score))
            or score < 0
            or score > 100
        ):
            continue
        candidate: dict[str, Any] = {
            "id": recording_id[:128],
            "title": title[:512],
            "artist": _artist_credit(recording.get("artist-credit"))[:512],
            "score": int(score),
        }
        releases = recording.get("releases")
        if isinstance(releases, list) and releases and isinstance(releases[0], dict):
            release_title = releases[0].get("title")
            if isinstance(release_title, str) and release_title:
                candidate["release"] = release_title[:512]
        candidates.append(candidate)
        if len(candidates) >= MUSICBRAINZ_MAX_CANDIDATES:
            break
    return candidates

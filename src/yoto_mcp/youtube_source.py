"""Verified local acquisition of public YouTube audio and source metadata."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from .metadata import format_track_title

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={}"


def parse_youtube_id(value: str) -> str:
    """Accept an exact video ID or a narrowly supported, unambiguous HTTPS link."""
    if not isinstance(value, str) or len(value) > 512 or any(char.isspace() for char in value):
        raise YouTubeSourceError("A canonical YouTube video ID or URL is required")
    if _VIDEO_ID_RE.fullmatch(value):
        return value
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.fragment or parts.netloc not in {
        "www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be",
    }:
        raise YouTubeSourceError("A supported HTTPS YouTube video URL is required")
    try:
        params = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=8)
    except ValueError:
        raise YouTubeSourceError("YouTube URL has invalid query parameters") from None
    if (len({key for key, _ in params}) != len(params)
            or any(key not in {"v", "si", "is", "t", "start", "feature"} for key, _ in params)):
        raise YouTubeSourceError("YouTube URL must identify one video, not a playlist")
    if parts.netloc == "youtu.be":
        video_id = parts.path.removeprefix("/") if parts.path.startswith("/") else ""
        if any(key == "v" for key, _ in params):
            raise YouTubeSourceError("YouTube URL contains a second video ID")
    elif parts.path == "/watch":
        ids = [item for key, item in params if key == "v"]
        video_id = ids[0] if len(ids) == 1 else ""
    elif parts.path.startswith("/shorts/"):
        video_id = parts.path.removeprefix("/shorts/")
        if any(key == "v" for key, _ in params):
            raise YouTubeSourceError("YouTube URL contains a second video ID")
    else:
        video_id = ""
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise YouTubeSourceError("YouTube URL has no exact video ID")
    return video_id
YT_DLP_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 180
MEDIA_PROCESS_TIMEOUT_SECONDS = 180
MAX_METADATA_OUTPUT_BYTES = 1024 * 1024
MAX_SOURCE_AUDIO_BYTES = 100 * 1024 * 1024
MAX_DURATION_SECONDS = 60 * 60
MAX_CHANNEL_PAGE_BYTES = 2 * 1024 * 1024
MAX_AVATAR_BYTES = 5 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 10.0
AVATAR_HOSTS = {"yt3.ggpht.com", "yt3.googleusercontent.com"}
ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
MUSICBRAINZ_RECORDING_URL = "https://musicbrainz.org/ws/2/recording/{}"
MUSICBRAINZ_USER_AGENT = "yoto-mcp/0.1.0 (local metadata lookup)"
MAX_FINGERPRINT_BYTES = 512 * 1024
MAX_METADATA_RESPONSE_BYTES = 1024 * 1024
FINGERPRINT_CONFIDENCE = 0.95
FINGERPRINT_SCORE_EPSILON = 0.02
_OFFICIAL_SUFFIX_RE = re.compile(
    r"\s*[\[(](?:official\s+(?:music\s+)?video|official\s+audio|"
    r"lyrics?|lyric\s+video|music\s+video|visuali[sz]er|"
    r"(?:4k|8k)\s+remaster(?:ed)?)[^\])]*[\])]\s*$",
    re.IGNORECASE,
)
_VOLUME_RE = re.compile(r"\bmax_volume:\s*(-inf|[-+]?\d+(?:\.\d+)?)\s*dB\b")
_MEAN_RE = re.compile(r"\bmean_volume:\s*(-inf|[-+]?\d+(?:\.\d+)?)\s*dB\b")


class YouTubeSourceError(RuntimeError):
    """Sanitized source acquisition or validation error."""


def _media_binary(name: str) -> str:
    """Use a vetted absolute executable, never a runtime PATH lookup."""
    key = "YOTO_FFMPEG" if name == "ffmpeg" else "YOTO_FFPROBE"
    configured = os.environ.get(key, f"/usr/bin/{name}")
    if not configured or not Path(configured).is_absolute():
        raise YouTubeSourceError(f"{name} requires an absolute configured path")
    try:
        binary = Path(configured).resolve(strict=True)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise OSError
    except (OSError, RuntimeError):
        raise YouTubeSourceError(f"Configured {name} executable is unavailable") from None
    return str(binary)


def _completed(
    runner: Callable[..., Any],
    args: list[str],
    *,
    timeout: int,
    output_limit: int = MAX_METADATA_OUTPUT_BYTES,
) -> Any:
    if runner is subprocess.run:
        return _bounded_capture(args, timeout=timeout, output_limit=output_limit)
    try:
        result = runner(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        raise YouTubeSourceError("The source or local media operation is unavailable or timed out") from None
    if getattr(result, "returncode", 1) != 0:
        raise YouTubeSourceError("The source or local media operation is unavailable or timed out")
    for field in ("stdout", "stderr"):
        value = getattr(result, field, None)
        if not isinstance(value, str) or len(value.encode("utf-8", errors="replace")) > output_limit:
            raise YouTubeSourceError("A source or media tool returned invalid or oversized output")
    return result


def _bounded_capture(args: list[str], *, timeout: int, output_limit: int) -> Any:
    """Capture a subprocess without buffering untrusted output past the limit."""
    try:
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
    except OSError:
        raise YouTubeSourceError("The source or local media operation is unavailable or timed out") from None
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise YouTubeSourceError("The source or local media operation is unavailable or timed out")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, min(65536, output_limit + 1 - len(output[key.data])))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output[key.data].extend(chunk)
                    if len(output[key.data]) > output_limit:
                        raise YouTubeSourceError("A source or media tool returned invalid or oversized output")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise YouTubeSourceError("The source or local media operation is unavailable or timed out")
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise YouTubeSourceError("The source or local media operation is unavailable or timed out") from None
        if code:
            raise YouTubeSourceError("The source or local media operation is unavailable or timed out")
        return subprocess.CompletedProcess(
            args,
            code,
            stdout=output["stdout"].decode("utf-8", errors="replace"),
            stderr=output["stderr"].decode("utf-8", errors="replace"),
        )
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _run_capture(
    runner: Callable[..., Any], args: list[str], *, timeout: int = YT_DLP_TIMEOUT_SECONDS,
    output_limit: int = MAX_METADATA_OUTPUT_BYTES,
) -> str:
    return _completed(runner, args, timeout=timeout, output_limit=output_limit).stdout


def _run_quiet(
    runner: Callable[..., Any], args: list[str], *, timeout: int = DOWNLOAD_TIMEOUT_SECONDS
) -> None:
    try:
        result = runner(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise YouTubeSourceError("The source or local media operation is unavailable or timed out") from None
    if getattr(result, "returncode", 1) != 0:
        raise YouTubeSourceError("The source or local media operation is unavailable or timed out")


def _probe_video(video_id: str, runner: Callable[..., Any]) -> dict[str, Any]:
    args = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-playlist",
        "--no-warnings",
        "--skip-download",
        "--dump-single-json",
        "--no-progress",
        YOUTUBE_WATCH_URL.format(video_id),
    ]
    raw = _run_capture(runner, args)
    try:
        info = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        raise YouTubeSourceError("YouTube returned invalid metadata") from None
    if not isinstance(info, dict) or info.get("id") != video_id:
        raise YouTubeSourceError("YouTube returned metadata for an unexpected video")
    source_duration = info.get("duration")
    if (
        isinstance(source_duration, (int, float))
        and not isinstance(source_duration, bool)
        and (not math.isfinite(source_duration) or source_duration > MAX_DURATION_SECONDS)
    ):
        raise YouTubeSourceError("YouTube source duration exceeds Yoto's one-hour limit")
    return info


def _make_staging_directory(upload_root: Path, video_id: str) -> Path:
    try:
        root = Path(upload_root).resolve(strict=True)
        if not root.is_dir():
            raise OSError
        staging_parent = root / ".youtube-jobs"
        staging_parent.mkdir(mode=0o700, exist_ok=True)
        staging_parent = staging_parent.resolve(strict=True)
        if not staging_parent.is_relative_to(root) or not staging_parent.is_dir():
            raise OSError
        staging_parent.chmod(0o700)
        stage = staging_parent / f"{video_id}-{secrets.token_hex(8)}"
        stage.mkdir(mode=0o700)
        stage.chmod(0o700)
        if not stage.resolve(strict=True).is_relative_to(root):
            raise OSError
        return stage
    except (OSError, RuntimeError):
        raise YouTubeSourceError("YOTO_UPLOAD_ROOT is unavailable for private staging") from None


def _download_source_audio(
    video_id: str, stage: Path, runner: Callable[..., Any]
) -> Path:
    output_template = stage / "source.%(ext)s"
    args = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--format",
        "bestaudio",
        "--max-filesize",
        "100M",
        "--output",
        str(output_template),
        YOUTUBE_WATCH_URL.format(video_id),
    ]
    _run_quiet(runner, args, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    files = list(stage.glob("source.*"))
    if len(files) != 1:
        raise YouTubeSourceError("YouTube did not produce exactly one audio source")
    source = files[0]
    try:
        resolved = source.resolve(strict=True)
        size = resolved.stat().st_size
        if (
            not resolved.is_relative_to(stage)
            or not resolved.is_file()
            or source.is_symlink()
            or size < 128
            or size > MAX_SOURCE_AUDIO_BYTES
        ):
            raise OSError
    except (OSError, RuntimeError):
        raise YouTubeSourceError("Downloaded YouTube audio failed local validation") from None
    return resolved


def _text_field(value: Any, *, limit: int = 300) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(char for char in value.strip() if ord(char) >= 32 and ord(char) != 127)
    return cleaned[:limit].strip()


def _fallback_metadata(source_title: str, channel_name: str) -> tuple[str, str, str]:
    clean_title = source_title
    while True:
        shortened = _OFFICIAL_SUFFIX_RE.sub("", clean_title).strip()
        if shortened == clean_title:
            break
        clean_title = shortened
    parts = re.split(r"\s+[-–—]\s+", clean_title)
    if len(parts) == 2 and all(part.strip() for part in parts):
        artist, title = (_text_field(part, limit=200) for part in parts)
        if artist and title:
            return artist, title, "video_title_artist_song"
    artist = _text_field(channel_name, limit=200)
    title = _text_field(clean_title, limit=300)
    return artist, title, "channel_title"


def _encode_mp3(
    source: Path,
    mp3_path: Path,
    artist: str,
    title: str,
    album: str,
    runner: Callable[..., Any],
) -> None:
    args = [
        _media_binary("ffmpeg"),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "2",
        "-metadata",
        f"artist={artist}",
        "-metadata",
        f"title={title}",
    ]
    if album:
        args.extend(("-metadata", f"album={album}"))
    args.extend(("-f", "mp3", str(mp3_path)))
    _run_quiet(runner, args, timeout=MEDIA_PROCESS_TIMEOUT_SECONDS)
    try:
        if not mp3_path.is_file() or mp3_path.is_symlink():
            raise OSError
        size = mp3_path.stat().st_size
        if size < 128 or size > MAX_SOURCE_AUDIO_BYTES:
            raise OSError
        mp3_path.chmod(0o600)
    except OSError:
        raise YouTubeSourceError("FFmpeg did not produce a valid bounded MP3") from None


def _verify_mp3(path: Path, runner: Callable[..., Any]) -> None:
    probe_args = [
        _media_binary("ffprobe"),
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_name",
        "-of",
        "json",
        str(path),
    ]
    raw = _run_capture(
        runner, probe_args, timeout=MEDIA_PROCESS_TIMEOUT_SECONDS, output_limit=64 * 1024
    )
    try:
        probe = json.loads(raw)
        streams = probe.get("streams")
        media_format = probe.get("format")
        duration = float(media_format.get("duration")) if isinstance(media_format, dict) else 0.0
        codecs = [stream.get("codec_name") for stream in streams if isinstance(stream, dict)]
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        raise YouTubeSourceError("FFprobe returned invalid MP3 metadata") from None
    if not isinstance(streams, list) or "mp3" not in codecs or not math.isfinite(duration):
        raise YouTubeSourceError("Extracted audio is not a valid MP3")
    if duration <= 0 or duration > MAX_DURATION_SECONDS:
        raise YouTubeSourceError("Extracted MP3 duration is outside the supported range")

    loudness_args = [
        _media_binary("ffmpeg"),
        "-nostdin",
        "-hide_banner",
        "-i",
        str(path),
        "-vn",
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    result = _completed(
        runner, loudness_args, timeout=MEDIA_PROCESS_TIMEOUT_SECONDS, output_limit=64 * 1024
    )
    max_match = _VOLUME_RE.search(result.stderr)
    mean_match = _MEAN_RE.search(result.stderr)
    if max_match is None or mean_match is None:
        raise YouTubeSourceError("FFmpeg could not verify source audio audibility")
    try:
        max_volume = float(max_match.group(1))
        mean_volume = float(mean_match.group(1))
    except ValueError:
        raise YouTubeSourceError("Extracted MP3 is silent or inaudible") from None
    if not math.isfinite(max_volume) or not math.isfinite(mean_volume) or mean_volume <= -60:
        raise YouTubeSourceError("Extracted MP3 is silent or inaudible")


def _fingerprint_audio(path: Path, runner: Callable[..., Any]) -> tuple[str, int]:
    # FFmpeg's Chromaprint muxer is available here; fpcalc is not installed.
    fingerprint = _run_capture(
        runner,
        [
            _media_binary("ffmpeg"), "-nostdin", "-v", "error", "-i", str(path),
            "-map", "0:a:0", "-vn", "-ac", "2",
            "-f", "chromaprint", "-fp_format", "base64", "-",
        ],
        timeout=MEDIA_PROCESS_TIMEOUT_SECONDS,
        output_limit=MAX_FINGERPRINT_BYTES,
    ).strip()
    probe = _run_capture(
        runner,
        [_media_binary("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        timeout=MEDIA_PROCESS_TIMEOUT_SECONDS,
        output_limit=64 * 1024,
    )
    try:
        duration = float(json.loads(probe)["format"]["duration"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError, RecursionError):
        raise YouTubeSourceError("Chromaprint source duration is invalid") from None
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]+", fingerprint)
        or len(fingerprint) > MAX_FINGERPRINT_BYTES
        or not math.isfinite(duration)
        or duration <= 0
        or duration > MAX_DURATION_SECONDS
    ):
        raise YouTubeSourceError("Chromaprint returned invalid audio data")
    return fingerprint, round(duration)


def _request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    limit: int = MAX_METADATA_RESPONSE_BYTES,
    data: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        with client.stream(
            method,
            url,
            data=data,
            params=params,
            headers={"Accept": "application/json"},
        ) as response:
            if response.status_code != 200:
                raise YouTubeSourceError("Public metadata service is unavailable")
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) < 0 or int(length) > limit:
                        raise YouTubeSourceError("Public metadata response exceeds size limit")
                except ValueError:
                    raise YouTubeSourceError("Public metadata response is invalid") from None
            body = bytearray()
            for chunk in response.iter_bytes():
                if len(body) + len(chunk) > limit:
                    raise YouTubeSourceError("Public metadata response exceeds size limit")
                body.extend(chunk)
    except YouTubeSourceError:
        raise
    except httpx.HTTPError:
        raise YouTubeSourceError("Public metadata service is unavailable") from None
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise YouTubeSourceError("Public metadata response is invalid") from None
    if not isinstance(parsed, dict):
        raise YouTubeSourceError("Public metadata response is invalid")
    return parsed


def _artist_credit(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts = []
    for item in value:
        if isinstance(item, dict):
            artist = item.get("artist")
            name = artist.get("name") if isinstance(artist, dict) else ""
            if isinstance(name, str):
                parts.append(name)
            joinphrase = item.get("joinphrase")
            if isinstance(joinphrase, str):
                parts.append(joinphrase)
        elif isinstance(item, str):
            parts.append(item)
    return _text_field("".join(parts), limit=300)


def _acoustid_artist(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    names = [
        item.get("name")
        for item in value
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    return _text_field(" & ".join(names), limit=300)


def _normalise_label(value: str) -> str:
    return " ".join(value.casefold().split())


def _recording_candidates(data: dict[str, Any]) -> list[dict[str, Any]]:
    if data.get("status") != "ok" or not isinstance(data.get("results"), list):
        raise YouTubeSourceError("AcoustID returned no usable match data")
    candidates_by_id: dict[str, dict[str, Any]] = {}
    for result in data["results"]:
        if not isinstance(result, dict):
            continue
        score = result.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or score < 0
            or score > 1
        ):
            continue
        recordings = result.get("recordings")
        if not isinstance(recordings, list):
            continue
        for recording in recordings:
            if not isinstance(recording, dict):
                continue
            recording_id = recording.get("id")
            title = _text_field(recording.get("title"), limit=300)
            artist = _acoustid_artist(recording.get("artists"))
            if (
                not isinstance(recording_id, str)
                or not re.fullmatch(r"[0-9a-fA-F-]{36}", recording_id)
            ):
                continue
            previous = candidates_by_id.get(recording_id)
            if previous is None or score > previous["score"]:
                candidates_by_id[recording_id] = {
                    "id": recording_id,
                    "artist": artist,
                    "title": title,
                    "score": float(score),
                }
    return sorted(candidates_by_id.values(), key=lambda item: item["score"], reverse=True)


def _musicbrainz_metadata(
    client: httpx.Client, recording: dict[str, Any]
) -> dict[str, str]:
    response = _request_json(
        client,
        "GET",
        MUSICBRAINZ_RECORDING_URL.format(recording["id"]),
        params={"fmt": "json", "inc": "artist-credits+releases"},
    )
    title = _text_field(response.get("title"), limit=300)
    artist = _artist_credit(response.get("artist-credit"))
    if not artist or not title:
        raise YouTubeSourceError("MusicBrainz recording metadata is incomplete")
    releases = response.get("releases")
    release_titles = {
        _text_field(release.get("title"), limit=300)
        for release in releases
        if isinstance(release, dict) and _text_field(release.get("title"), limit=300)
    } if isinstance(releases, list) else set()
    album = next(iter(release_titles)) if len(release_titles) == 1 else ""
    return {"artist": artist, "title": title, "album": album}


def _identify_fingerprint(
    client: httpx.Client,
    key: str,
    fingerprint: str,
    duration: int,
) -> tuple[dict[str, Any] | None, list[str], bool]:
    warnings: list[str] = []
    try:
        response = _request_json(
            client,
            "POST",
            ACOUSTID_LOOKUP_URL,
            data={
                "client": key,
                "duration": str(duration),
                "fingerprint": fingerprint,
                "meta": "recordingids",
                "format": "json",
            },
        )
        candidates = _recording_candidates(response)
    except YouTubeSourceError:
        return None, ["AcoustID lookup failed; using conservative video metadata"], False
    if not candidates or candidates[0]["score"] < FINGERPRINT_CONFIDENCE:
        return None, [], False
    top_score = candidates[0]["score"]
    plausible = [
        item for item in candidates
        if top_score - item["score"] <= FINGERPRINT_SCORE_EPSILON
    ]
    if len(plausible) > 1 and any(not item["artist"] or not item["title"] for item in plausible):
        return None, ["Equally strong fingerprint recordings lack labels; using video metadata"], False
    label_pairs = {
        (_normalise_label(item["artist"]), _normalise_label(item["title"]))
        for item in plausible
    }
    if len(label_pairs) > 1:
        return None, ["Equally strong fingerprint recordings disagree; using video metadata"], False
    selected = candidates[0]
    if len(plausible) > 1:
        return {
            "artist": selected["artist"],
            "title": selected["title"],
            "album": "",
            "metadata_source": "acoustid_consensus",
            "metadata_score": top_score,
        }, warnings, False
    try:
        musicbrainz = _musicbrainz_metadata(client, selected)
    except YouTubeSourceError:
        if not selected["artist"] or not selected["title"]:
            return None, ["MusicBrainz recording details unavailable; using video metadata"], False
        return {
            "artist": selected["artist"],
            "title": selected["title"],
            "album": "",
            "metadata_source": "acoustid",
            "metadata_score": top_score,
        }, ["MusicBrainz recording details unavailable; using AcoustID labels"], False
    return {
        **musicbrainz,
        "metadata_source": "acoustid_musicbrainz",
        "metadata_score": top_score,
    }, warnings, False


class _ChannelPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.channel_id = ""
        self.channel_name = ""
        self.og_image = ""
        self.identity_urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "meta":
            prop = values.get("property", "").lower()
            itemprop = values.get("itemprop", "").lower()
            content = values.get("content", "").strip()
            if itemprop == "channelid":
                self.channel_id = content
            elif (itemprop in {"name", "channelname"} and content) or (
                prop == "og:title" and content and not self.channel_name
            ):
                self.channel_name = content
            elif prop == "og:image":
                self.og_image = content
            elif prop == "og:url":
                self.identity_urls.append(content)
        elif tag == "link" and "canonical" in values.get("rel", "").lower().split():
            self.identity_urls.append(values.get("href", ""))


def _read_bounded_response(
    client: httpx.Client, url: str, *, limit: int, accept: str
) -> tuple[bytes, httpx.Headers]:
    try:
        with client.stream("GET", url, headers={"Accept": accept}) as response:
            if response.status_code != 200:
                raise YouTubeSourceError("YouTube channel or avatar source is unavailable")
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) < 0 or int(length) > limit:
                        raise YouTubeSourceError("YouTube channel or avatar source exceeds size limit")
                except ValueError:
                    raise YouTubeSourceError("YouTube channel or avatar response is invalid") from None
            body = bytearray()
            for chunk in response.iter_bytes():
                if len(body) + len(chunk) > limit:
                    raise YouTubeSourceError("YouTube channel or avatar source exceeds size limit")
                body.extend(chunk)
            return bytes(body), response.headers
    except YouTubeSourceError:
        raise
    except httpx.HTTPError:
        raise YouTubeSourceError("YouTube channel or avatar source is unavailable") from None


def _url_has_channel_identity(value: str, channel_id: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"youtube.com", "www.youtube.com"}
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == f"/channel/{channel_id}"
    )


def _avatar_target(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise YouTubeSourceError("Channel avatar URL is invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in AVATAR_HOSTS
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise YouTubeSourceError("Channel avatar host is not allowed")
    return value, parsed.hostname or ""


def _download_avatar(
    client: httpx.Client, stage: Path, channel_id: str, channel_name: str
) -> Path:
    if not _CHANNEL_ID_RE.fullmatch(channel_id) or not channel_name:
        raise YouTubeSourceError("Channel identity is incomplete")
    page_url = f"https://www.youtube.com/channel/{channel_id}"
    page_bytes, _ = _read_bounded_response(
        client,
        page_url,
        limit=MAX_CHANNEL_PAGE_BYTES,
        accept="text/html",
    )
    parser = _ChannelPageParser()
    try:
        parser.feed(page_bytes.decode("utf-8", errors="replace"))
        parser.close()
    except (ValueError, RecursionError):
        raise YouTubeSourceError("YouTube channel page is invalid") from None
    identity_match = parser.channel_id == channel_id or any(
        _url_has_channel_identity(url, channel_id) for url in parser.identity_urls
    )
    if not identity_match or not parser.channel_name:
        raise YouTubeSourceError("YouTube channel page identity could not be verified")
    # Topic channels may use a suffixed page name even when yt-dlp reports the
    # plain artist. The exact channel ID, not display-name equality, is authority.
    image_url, image_host = _avatar_target(parser.og_image)
    image, headers = _read_bounded_response(
        client,
        image_url,
        limit=MAX_AVATAR_BYTES,
        accept="image/png,image/jpeg,image/gif",
    )
    mime = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    types = (
        (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
        (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
        (b"GIF87a", "image/gif", ".gif"),
        (b"GIF89a", "image/gif", ".gif"),
    )
    matched = next(((kind, suffix) for signature, kind, suffix in types if image.startswith(signature)), None)
    if len(image) < 12 or matched is None or (mime and mime != matched[0]):
        raise YouTubeSourceError("Channel avatar content is not a supported image")
    target = stage / f"channel-avatar{matched[1]}"
    try:
        with target.open("xb") as output:
            output.write(image)
        target.chmod(0o600)
    except OSError:
        raise YouTubeSourceError("Channel avatar could not be staged privately") from None
    if image_host not in AVATAR_HOSTS:
        raise YouTubeSourceError("Channel avatar host is not allowed")
    return target


def prepare_youtube(
    video_id: str,
    upload_root: Path,
    acoustid_key: str | None = None,
    *,
    artist: str | None = None,
    song_name: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Prepare a bounded local MP3 and verified public-source metadata."""
    if not isinstance(video_id, str) or not _VIDEO_ID_RE.fullmatch(video_id):
        raise YouTubeSourceError("A canonical YouTube video ID is required")
    if (artist is None) != (song_name is None):
        raise ValueError("artist and song_name must be provided together")
    override_artist: str | None = None
    override_title: str | None = None
    if artist is not None and song_name is not None:
        if any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            for value in (artist, song_name)
        ):
            raise ValueError("artist and song_name must be nonempty, printable, and at most 200 characters")
        override_artist, override_title = artist.strip(), song_name.strip()
    explicit_override = override_artist is not None
    info = _probe_video(video_id, runner)
    raw_title = info.get("title")
    source_title = raw_title if isinstance(raw_title, str) else ""
    if not source_title.strip() or len(source_title) > 1000:
        raise YouTubeSourceError("YouTube returned no usable video title")
    channel_name = _text_field(info.get("channel") or info.get("uploader"), limit=200)
    channel_id = info.get("channel_id", "")
    if not isinstance(channel_id, str) or not _CHANNEL_ID_RE.fullmatch(channel_id):
        channel_id = ""
    fallback_artist, fallback_title, fallback_source = _fallback_metadata(source_title, channel_name)
    artist = override_artist if override_artist is not None else fallback_artist
    title = override_title if override_title is not None else fallback_title
    metadata_source = "user_override" if explicit_override else fallback_source
    album = ""
    metadata_score = None
    requires_review = False
    warnings: list[str] = []
    key = "" if explicit_override else (
        acoustid_key if acoustid_key is not None else os.environ.get("ACOUSTID_API_KEY", "")
    )
    if not isinstance(key, str) or len(key) > 512 or any(ord(char) < 32 for char in key):
        key = ""
        warnings.append("AcoustID lookup skipped because the configured API key is invalid")
    elif not key and not explicit_override:
        warnings.append("AcoustID lookup unavailable: no API key configured")
    stage = _make_staging_directory(upload_root, video_id)
    source_audio = _download_source_audio(video_id, stage, runner)
    mp3_path = stage / "audio.mp3"
    client = httpx.Client(
        transport=transport,
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={"User-Agent": MUSICBRAINZ_USER_AGENT},
    )
    avatar_path: Path | None = None
    try:
        with client:
            if key:
                try:
                    fingerprint, duration = _fingerprint_audio(source_audio, runner)
                except YouTubeSourceError:
                    warnings.append("Chromaprint generation failed; using conservative video metadata")
                else:
                    match, match_warnings, requires_review = _identify_fingerprint(
                        client, key, fingerprint, duration
                    )
                    warnings.extend(match_warnings)
                    if match is None and not match_warnings:
                        warnings.append(
                            "No unique high-confidence recording match; using conservative video metadata"
                        )
                    elif match is not None:
                        artist = match["artist"]
                        title = match["title"]
                        album = match["album"]
                        metadata_source = match["metadata_source"]
                        metadata_score = match["metadata_score"]
                        if (
                            _normalise_label(artist) != _normalise_label(fallback_artist)
                            or _normalise_label(title) != _normalise_label(fallback_title)
                        ):
                            warnings.append(
                                "Video title/uploader differs from fingerprint identification; source audio is unchanged"
                            )
            _encode_mp3(source_audio, mp3_path, artist, title, album, runner)
            source_audio.unlink()
            _verify_mp3(mp3_path, runner)
            try:
                avatar_path = _download_avatar(client, stage, channel_id, channel_name)
            except YouTubeSourceError:
                warnings.append("Channel avatar unavailable or could not be verified")
    finally:
        client.close()
    return {
        "video_id": video_id,
        "source_title": source_title,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "artist": artist,
        "title": title,
        "album": album,
        "title_label": format_track_title(artist, title),
        "metadata_source": metadata_source,
        "metadata_score": metadata_score,
        "warnings": warnings,
        "mp3_path": str(mp3_path),
        "avatar_path": str(avatar_path) if avatar_path else None,
        "requires_review": requires_review,
    }

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from yoto_mcp.youtube_source import (
    YouTubeSourceError,
    _completed,
    _download_avatar,
    _fallback_metadata,
    _fingerprint_audio,
    _identify_fingerprint,
    prepare_youtube,
)

CHANNEL_ID = "UC" + "a" * 22


def _channel_transport(channel_name: str = "Test Channel", channel_id: str = CHANNEL_ID):
    image = b"\x89PNG\r\n\x1a\nfixture-image"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.youtube.com":
            assert request.url.path == f"/channel/{channel_id}"
            page = (
                f'<meta itemprop="channelId" content="{channel_id}">'
                f'<meta itemprop="name" content="{channel_name}">'
                '<meta property="og:image" content="https://yt3.ggpht.com/avatar.png">'
            )
            return httpx.Response(200, text=page, request=request)
        if request.url.host == "yt3.ggpht.com":
            return httpx.Response(200, content=image, headers={"content-type": "image/png"}, request=request)
        raise AssertionError(f"unexpected host: {request.url.host}")

    return httpx.MockTransport(handler)


def _fingerprint_transport(requests):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.acoustid.org":
            assert request.method == "POST"
            form = parse_qs(request.content.decode("ascii"))
            assert form["client"] == ["test-key"]
            assert form["fingerprint"] == ["FINGERPRINT-FIXTURE"]
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "results": [{
                        "score": 0.99,
                        "recordings": [{"id": "123e4567-e89b-12d3-a456-426614174000"}],
                    }],
                },
                request=request,
            )
        if request.url.host == "musicbrainz.org":
            assert request.url.path == "/ws/2/recording/123e4567-e89b-12d3-a456-426614174000"
            assert request.url.params.get("inc") == "artist-credits+releases"
            return httpx.Response(
                200,
                json={
                    "title": "Song",
                    "artist-credit": [{"artist": {"name": "Original Artist"}}],
                    "releases": [{"title": "Verified Album"}],
                },
                request=request,
            )
        if request.url.host == "www.youtube.com":
            return httpx.Response(
                200,
                text=(
                    f'<meta itemprop="channelId" content="{CHANNEL_ID}">'
                    '<meta itemprop="name" content="Test Channel">'
                    '<meta property="og:image" content="https://yt3.ggpht.com/avatar.png">'
                ),
                request=request,
            )
        if request.url.host == "yt3.ggpht.com":
            return httpx.Response(
                200,
                content=b"\x89PNG\r\n\x1a\nfixture-image",
                headers={"content-type": "image/png"},
                request=request,
            )
        raise AssertionError(f"unexpected host: {request.url.host}")

    return httpx.MockTransport(handler)


def _ambiguous_fingerprint_transport(requests):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.acoustid.org":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "results": [
                        {
                            "score": 0.99,
                            "recordings": [{
                                "id": "123e4567-e89b-12d3-a456-426614174000",
                                "title": "Song",
                                "artists": [{"name": "Artist One"}],
                            }],
                        },
                        {
                            "score": 0.985,
                            "recordings": [{
                                "id": "223e4567-e89b-12d3-a456-426614174000",
                                "title": "Different Song",
                                "artists": [{"name": "Artist Two"}],
                            }],
                        },
                    ],
                },
                request=request,
            )
        if request.url.host == "musicbrainz.org":
            return httpx.Response(
                200,
                json={
                    "title": "Song",
                    "artist-credit": [{"artist": {"name": "Artist One"}}],
                    "releases": [],
                },
                request=request,
            )
        if request.url.host == "www.youtube.com":
            return httpx.Response(
                200,
                text=(
                    f'<meta itemprop="channelId" content="{CHANNEL_ID}">'
                    '<meta itemprop="name" content="Test Channel">'
                    '<meta property="og:image" content="https://yt3.ggpht.com/avatar.png">'
                ),
                request=request,
            )
        if request.url.host == "yt3.ggpht.com":
            return httpx.Response(
                200,
                content=b"\x89PNG\r\n\x1a\nfixture-image",
                headers={"content-type": "image/png"},
                request=request,
            )
        raise AssertionError(f"unexpected host: {request.url.host}")

    return httpx.MockTransport(handler)


def test_rejects_noncanonical_video_ids_before_external_calls(tmp_path: Path):
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("invalid IDs must be rejected before subprocess execution")

    with pytest.raises(YouTubeSourceError, match="canonical YouTube video ID"):
        prepare_youtube("https://youtube.com/watch?v=abcdefghijk", tmp_path, runner=runner)

    assert calls == []


def test_yt_dlp_metadata_probe_uses_only_the_canonical_video_url(tmp_path: Path):
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    with pytest.raises(YouTubeSourceError, match="unavailable or timed out"):
        prepare_youtube("abcdefghijk", tmp_path, runner=runner)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:3] == [sys.executable, "-m", "yt_dlp"]
    assert "--ignore-config" in args
    assert "--no-playlist" in args
    assert "--dump-single-json" in args
    assert args[-1] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] > 0


def test_audio_download_is_private_bounded_and_tied_to_the_same_id(tmp_path: Path):
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        if "--dump-single-json" in args:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({
                    "id": "abcdefghijk",
                    "title": "Artist - Song",
                    "channel": "Test Channel",
                    "channel_id": "UC" + "a" * 22,
                }),
                stderr="",
            )
        if "--format" in args:
            output_template = Path(args[args.index("--output") + 1])
            source_audio = Path(str(output_template).replace("%(ext)s", "m4a"))
            source_audio.write_bytes(b"s" * 256)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if Path(args[0]).name == "ffmpeg" and "libmp3lame" in args:
            Path(args[-1]).write_bytes(b"m" * 256)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if Path(args[0]).name == "ffprobe":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({"streams": [{"codec_name": "mp3"}], "format": {"duration": "123"}}),
                stderr="",
            )
        if Path(args[0]).name == "ffmpeg" and "volumedetect" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="", stderr="mean_volume: -20.0 dB\nmax_volume: -1.0 dB"
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    prepare_youtube("abcdefghijk", tmp_path, runner=runner, transport=_channel_transport())

    assert len(calls) == 5
    args, kwargs = calls[1]
    assert "--ignore-config" in args
    assert "--no-playlist" in args
    assert "--max-filesize" in args
    assert args[args.index("--max-filesize") + 1].endswith("M")
    assert args[args.index("--format") + 1] == "bestaudio"
    assert args[-1] == "https://www.youtube.com/watch?v=abcdefghijk"
    stage_path = Path(args[args.index("--output") + 1]).parent
    assert stage_path.is_relative_to(tmp_path.resolve())
    assert kwargs["timeout"] > 0
    assert "shell" not in kwargs


def test_source_audio_becomes_tagged_and_verified_mp3(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ACOUSTID_API_KEY", raising=False)
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        if "--dump-single-json" in args:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({
                    "id": "abcdefghijk",
                    "title": "Artist - Song (Official Video)",
                    "channel": "Test Channel",
                    "channel_id": "UC" + "a" * 22,
                }),
                stderr="",
            )
        if "--format" in args:
            output_template = Path(args[args.index("--output") + 1])
            Path(str(output_template).replace("%(ext)s", "m4a")).write_bytes(b"s" * 256)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if Path(args[0]).name == "ffmpeg" and "libmp3lame" in args:
            Path(args[-1]).write_bytes(b"m" * 256)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if Path(args[0]).name == "ffprobe":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({
                    "streams": [{"codec_name": "mp3"}],
                    "format": {"duration": "123.0", "tags": {"artist": "Artist", "title": "Song"}},
                }),
                stderr="",
            )
        if Path(args[0]).name == "ffmpeg" and "volumedetect" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="",
                stderr="[Parsed_volumedetect_0 @ 0xabc] mean_volume: -20.0 dB\n"
                       "[Parsed_volumedetect_0 @ 0xabc] max_volume: -1.0 dB"
            )
        raise AssertionError(f"unexpected subprocess: {args[0]}")

    result = prepare_youtube(
        "abcdefghijk", tmp_path, runner=runner, transport=_channel_transport()
    )

    assert result["artist"] == "Artist"
    assert result["title"] == "Song"
    assert result["album"] == ""
    assert result["requires_review"] is False
    assert "AcoustID lookup unavailable: no API key configured" in result["warnings"]
    assert result["source_title"] == "Artist - Song (Official Video)"
    mp3_path = Path(result["mp3_path"])
    assert mp3_path.is_relative_to(tmp_path.resolve())
    assert mp3_path.is_file()
    assert mp3_path.stat().st_size <= 100 * 1024 * 1024
    assert Path(result["avatar_path"]).is_file()
    assert any(Path(args[0]).name == "ffprobe" for args, _ in calls)
    encode_args = next(args for args, _ in calls if Path(args[0]).name == "ffmpeg" and "libmp3lame" in args)
    assert "-metadata" in encode_args
    assert "artist=Artist" in encode_args
    assert "title=Song" in encode_args


def test_strong_fingerprint_metadata_wins_conflicting_video_credits(tmp_path: Path):
    requests = []
    process_calls = []

    def runner(args, **kwargs):
        process_calls.append((args, kwargs))
        if "--dump-single-json" in args:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({
                    "id": "abcdefghijk",
                    "title": "Video Artist - Song (Live Version)",
                    "channel": "Test Channel",
                    "channel_id": CHANNEL_ID,
                    "tags": ["untrusted uploader tag"],
                }),
                stderr="",
            )
        if "--format" in args:
            template = Path(args[args.index("--output") + 1])
            Path(str(template).replace("%(ext)s", "m4a")).write_bytes(b"s" * 256)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if Path(args[0]).name == "ffmpeg" and "chromaprint" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="FINGERPRINT-FIXTURE", stderr=""
            )
        if Path(args[0]).name == "ffmpeg" and "libmp3lame" in args:
            Path(args[-1]).write_bytes(b"m" * 256)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if Path(args[0]).name == "ffprobe":
            return subprocess.CompletedProcess(
                args, 0,
                stdout=json.dumps({"streams": [{"codec_name": "mp3"}], "format": {"duration": "123"}}),
                stderr="",
            )
        if Path(args[0]).name == "ffmpeg" and "volumedetect" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="", stderr="mean_volume: -20.0 dB\nmax_volume: -1.0 dB"
            )
        raise AssertionError(f"unexpected subprocess: {args[0]}")

    result = prepare_youtube(
        "abcdefghijk",
        tmp_path,
        acoustid_key="test-key",
        runner=runner,
        transport=_fingerprint_transport(requests),
    )

    assert result["artist"] == "Original Artist"
    assert result["title"] == "Song"
    assert result["album"] == "Verified Album"
    assert result["source_title"] == "Video Artist - Song (Live Version)"
    assert result["metadata_source"] == "acoustid_musicbrainz"
    assert result["metadata_score"] == pytest.approx(0.99)
    assert any("differs from fingerprint identification" in item for item in result["warnings"])
    assert len(requests) == 4
    encode_args = next(args for args, _ in process_calls if Path(args[0]).name == "ffmpeg" and "libmp3lame" in args)
    assert "artist=Original Artist" in encode_args
    assert "title=Song" in encode_args
    assert "album=Verified Album" in encode_args


def test_equally_plausible_disagreeing_recordings_use_video_metadata_without_review(tmp_path: Path):
    requests = []

    def runner(args, **kwargs):
        if "--dump-single-json" in args:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({
                    "id": "abcdefghijk",
                    "title": "Video Artist - Song",
                    "channel": "Test Channel",
                    "channel_id": CHANNEL_ID,
                }),
                stderr="",
            )
        if "--format" in args:
            template = Path(args[args.index("--output") + 1])
            Path(str(template).replace("%(ext)s", "m4a")).write_bytes(b"s" * 256)
        elif Path(args[0]).name == "ffmpeg" and "chromaprint" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="FINGERPRINT-FIXTURE", stderr=""
            )
        elif Path(args[0]).name == "ffmpeg" and "libmp3lame" in args:
            Path(args[-1]).write_bytes(b"m" * 256)
        elif Path(args[0]).name == "ffprobe":
            return subprocess.CompletedProcess(
                args, 0,
                stdout=json.dumps({"streams": [{"codec_name": "mp3"}], "format": {"duration": "123"}}),
                stderr="",
            )
        elif Path(args[0]).name == "ffmpeg" and "volumedetect" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="", stderr="mean_volume: -20.0 dB\nmax_volume: -1.0 dB"
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    result = prepare_youtube(
        "abcdefghijk",
        tmp_path,
        acoustid_key="test-key",
        runner=runner,
        transport=_ambiguous_fingerprint_transport(requests),
    )

    assert result["requires_review"] is False
    assert result["metadata_source"] == "video_title_artist_song"
    assert result["artist"] == "Video Artist"
    assert result["title"] == "Song"
    assert result["metadata_score"] is None
    assert any("disagree" in item and "video metadata" in item for item in result["warnings"])

def test_explicit_artist_and_song_name_override_fingerprint_and_keep_source(tmp_path: Path, monkeypatch):
    from yoto_mcp import youtube_source as source

    monkeypatch.setattr(source, "_probe_video", lambda *_: {
        "id": "abcdefghijk", "title": "Video Artist - Original Title",
        "channel": "Test Channel", "channel_id": CHANNEL_ID,
    })

    def download(_video_id, stage, _runner):
        path = stage / "source.m4a"
        path.write_bytes(b"a" * 256)
        return path

    def encode(_source, target, artist, title, album, _runner):
        assert (artist, title, album) == ("Chosen Artist", "Chosen Song", "")
        target.write_bytes(b"m" * 256)

    def avatar(_client, stage, _channel_id, _channel_name):
        path = stage / "avatar.png"
        path.write_bytes(b"image")
        return path

    monkeypatch.setattr(source, "_download_source_audio", download)
    monkeypatch.setattr(source, "_encode_mp3", encode)
    monkeypatch.setattr(source, "_verify_mp3", lambda *_: None)
    monkeypatch.setattr(source, "_download_avatar", avatar)
    monkeypatch.setattr(source, "_fingerprint_audio", lambda *_: pytest.fail("override should skip fingerprint"))

    result = prepare_youtube(
        "abcdefghijk", tmp_path, acoustid_key="test-key", artist="Chosen Artist",
        song_name="Chosen Song", runner=lambda *_: pytest.fail("unexpected subprocess"),
    )
    assert (result["artist"], result["title"]) == ("Chosen Artist", "Chosen Song")
    assert result["metadata_source"] == "user_override"
    assert result["source_title"] == "Video Artist - Original Title"
    assert result["requires_review"] is False
    assert result["title_label"] == "Chosen Artist — Chosen Song"
    assert not result["warnings"]

def test_partial_explicit_credits_rejected_before_external_calls(tmp_path: Path):
    with pytest.raises(ValueError, match="artist and song_name"):
        prepare_youtube("abcdefghijk", tmp_path, artist="Only Artist")
    with pytest.raises(ValueError, match="artist and song_name"):
        prepare_youtube("abcdefghijk", tmp_path, song_name="Only Song")


def test_avatar_rejects_a_channel_page_with_a_different_identity(tmp_path: Path):
    requests = []
    other_channel_id = "UC" + "b" * 22

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "www.youtube.com":
            return httpx.Response(
                200,
                text=(
                    f'<meta itemprop="channelId" content="{other_channel_id}">'
                    '<meta itemprop="name" content="Test Channel">'
                    '<meta property="og:image" content="https://yt3.ggpht.com/avatar.png">'
                ),
                request=request,
            )
        if request.url.host == "yt3.ggpht.com":
            return httpx.Response(
                200,
                content=b"\x89PNG\r\n\x1a\nfixture-image",
                headers={"content-type": "image/png"},
                request=request,
            )
        raise AssertionError(f"unexpected host: {request.url.host}")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(YouTubeSourceError, match="identity could not be verified"),
    ):
        _download_avatar(client, tmp_path, CHANNEL_ID, "Test Channel")

    assert [request.url.host for request in requests] == ["www.youtube.com"]


def test_capture_stops_a_process_at_the_output_limit(tmp_path: Path):
    marker = tmp_path / "finished-after-overflow"
    script = (
        "import os,time,pathlib; os.write(1,b'x'*100000); time.sleep(0.3); "
        f"pathlib.Path({str(marker)!r}).touch()"
    )

    with pytest.raises(YouTubeSourceError):
        _completed(
            subprocess.run,
            [sys.executable, "-c", script],
            timeout=5,
            output_limit=64,
        )

    assert not marker.exists()


def test_media_tools_use_trusted_absolute_paths_instead_of_runtime_path(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setenv("PATH", str(tmp_path))

    def runner(args, **_kwargs):
        calls.append(args[0])
        if Path(args[0]).name == "ffmpeg":
            return subprocess.CompletedProcess(args, 0, stdout="AQAB-FINGERPRINT", stderr="")
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"format": {"duration": "172"}}), stderr="",
        )

    _fingerprint_audio(tmp_path / "audio.m4a", runner)
    assert calls == ["/usr/bin/ffmpeg", "/usr/bin/ffprobe"]
    monkeypatch.setenv("YOTO_FFMPEG", "ffmpeg")
    with pytest.raises(YouTubeSourceError, match="absolute"):
        _fingerprint_audio(tmp_path / "audio.m4a", runner)
    assert len(calls) == 2


def test_fingerprint_uses_installed_ffmpeg_chromaprint_without_fpcalc(tmp_path: Path):
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if Path(args[0]).name == "ffmpeg":
            assert args[args.index("-f") + 1] == "chromaprint"
            return subprocess.CompletedProcess(args, 0, stdout="AQAB-FINGERPRINT", stderr="")
        if Path(args[0]).name == "ffprobe":
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps({"format": {"duration": "171.933"}}), stderr=""
            )
        raise AssertionError("fpcalc is not installed or packaged")

    fingerprint, duration = _fingerprint_audio(tmp_path / "source.m4a", runner)

    assert fingerprint == "AQAB-FINGERPRINT"
    assert duration == 172
    assert [Path(args[0]).name for args in calls] == ["ffmpeg", "ffprobe"]


def test_title_fallback_strips_rickroll_video_descriptors_but_preserves_clean_version():
    artist, title, source = _fallback_metadata(
        "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)",
        "Rick Astley",
    )
    assert (artist, title, source) == (
        "Rick Astley", "Never Gonna Give You Up", "video_title_artist_song"
    )

    _, clean_title, _ = _fallback_metadata("Singer - A Song (Clean)", "Singer")
    assert clean_title == "A Song (Clean)"


def test_invalid_utf8_acoustid_response_falls_back_with_warning():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.acoustid.org"
        return httpx.Response(200, content=b"\xff")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        match, warnings, review = _identify_fingerprint(client, "throwaway-key", "fingerprint", 200)
    assert match is None and review is False
    assert warnings == ["AcoustID lookup failed; using conservative video metadata"]


def test_supported_youtube_links_extract_one_exact_video_id_without_playlist_guessing():
    from yoto_mcp import youtube_source as source

    video_id = "dQw4w9WgXcQ"
    for raw in (
        video_id,
        f"https://www.youtube.com/watch?v={video_id}",
        f"https://youtu.be/{video_id}?si=share-token",
        f"https://www.youtube.com/shorts/{video_id}",
    ):
        assert source.parse_youtube_id(raw) == video_id
    for raw in (
        f"http://www.youtube.com/watch?v={video_id}",
        f"https://www.youtube.com/watch?v={video_id}&list=playlist",
        f"https://www.youtube.com/watch?v={video_id}&v=abcdefghijk",
        f"https://www.youtube.com.evil.test/watch?v={video_id}",
        f"https://youtu.be/{video_id}/extra",
        f"https://user@youtu.be/{video_id}",
    ):
        with pytest.raises(YouTubeSourceError, match="YouTube"):
            source.parse_youtube_id(raw)


def test_fingerprint_score_below_high_confidence_does_not_override_video_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.acoustid.org"
        return httpx.Response(200, json={
            "status": "ok",
            "results": [{"score": 0.90, "recordings": [{
                "id": "123e4567-e89b-12d3-a456-426614174000",
            }]}],
        }, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        match, warnings, review = _identify_fingerprint(client, "test-key", "AQAB", 172)

    assert match is None
    assert review is False
    assert warnings == []


def test_avatar_accepts_topic_display_name_when_channel_id_matches(tmp_path: Path):
    with httpx.Client(transport=_channel_transport("Ylvis - Topic")) as client:
        image = _download_avatar(client, tmp_path, CHANNEL_ID, "Ylvis")

    assert image.is_file()


def test_source_rejects_video_longer_than_yoto_limit_before_download(tmp_path: Path):
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        assert "--dump-single-json" in args, "overlong source must not be downloaded"
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({
            "id": "abcdefghijk", "title": "Too long", "duration": 3601,
        }), stderr="")

    with pytest.raises(YouTubeSourceError, match="duration"):
        prepare_youtube("abcdefghijk", tmp_path, runner=runner)

    assert len(calls) == 1


def test_ranged_source_is_trimmed_before_fingerprinting_and_mp3_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ACOUSTID_API_KEY", raising=False)
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if "--dump-single-json" in args:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({
                "id": "abcdefghijk", "title": "Artist - Song", "channel": "Test Channel",
                "channel_id": CHANNEL_ID,
            }), stderr="")
        if "--format" in args:
            template = Path(args[args.index("--output") + 1])
            Path(str(template).replace("%(ext)s", "m4a")).write_bytes(b"s" * 256)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if Path(args[0]).name == "ffprobe":
            path = Path(args[-1])
            duration, codec = (
                (120, "aac") if path.suffix == ".m4a"
                else (10, "flac") if path.suffix == ".flac"
                else (10.026, "mp3")
            )
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({
                "streams": [{"codec_name": codec}], "format": {"duration": str(duration)},
            }), stderr="")
        if Path(args[0]).name == "ffmpeg" and "flac" in args:
            Path(args[-1]).write_bytes(b"f" * 256)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if Path(args[0]).name == "ffmpeg" and "chromaprint" in args:
            return subprocess.CompletedProcess(args, 0, stdout="FINGERPRINT-FIXTURE", stderr="")
        if Path(args[0]).name == "ffmpeg" and "libmp3lame" in args:
            Path(args[-1]).write_bytes(b"m" * 256)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if Path(args[0]).name == "ffmpeg" and "volumedetect" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="", stderr="mean_volume: -20.0 dB\nmax_volume: -1.0 dB",
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    result = prepare_youtube(
        "abcdefghijk", tmp_path, acoustid_key="test-key", start_ms=20_000, end_ms=30_000,
        runner=runner, transport=_fingerprint_transport([]),
    )

    trim_index = next(i for i, args in enumerate(calls) if "flac" in args)
    fingerprint_index = next(i for i, args in enumerate(calls) if "chromaprint" in args)
    encode_index = next(i for i, args in enumerate(calls) if "libmp3lame" in args)
    trim_args = calls[trim_index]
    assert trim_index < fingerprint_index < encode_index
    assert trim_args[trim_args.index("-ss") + 1] == "20.000"
    assert trim_args[trim_args.index("-t") + 1] == "10.000"
    assert result["source_interval"] == {
        "start_ms": 20_000, "end_ms": 30_000, "effective_duration_ms": 10_000,
    }
    assert Path(result["mp3_path"]).is_file()
    stage = Path(result["mp3_path"]).parent
    assert not list(stage.glob("source.*"))
    assert not (stage / "trimmed.flac").exists()


def test_requested_end_is_checked_against_downloaded_audio_duration(tmp_path: Path):
    from yoto_mcp import youtube_source as source

    source_audio = tmp_path / "source.m4a"
    source_audio.write_bytes(b"s" * 256)
    stage = tmp_path / "stage"
    stage.mkdir()
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({
            "streams": [{"codec_name": "aac"}], "format": {"duration": "10.0"},
        }), stderr="")

    with pytest.raises(YouTubeSourceError, match="decoded audio duration"):
        source._trim_source_audio(source_audio, stage, 0, 10_001, runner)

    assert len(calls) == 1
    assert not (stage / "trimmed.flac").exists()


def test_trim_rejects_a_clip_half_a_second_short_of_requested_interval(tmp_path: Path):
    from yoto_mcp import youtube_source as source

    original = tmp_path / "source.m4a"
    original.write_bytes(b"s" * 256)
    stage = tmp_path / "stage"
    stage.mkdir()

    def runner(args, **kwargs):
        if Path(args[0]).name == "ffprobe":
            is_trimmed = Path(args[-1]).suffix == ".flac"
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({
                "streams": [{"codec_name": "flac" if is_trimmed else "aac"}],
                "format": {"duration": "9.5" if is_trimmed else "120.0"},
            }), stderr="")
        if Path(args[0]).name == "ffmpeg" and "flac" in args:
            Path(args[-1]).write_bytes(b"f" * 256)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if Path(args[0]).name == "ffmpeg" and "null" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError("unexpected subprocess after a short decoded clip")

    with pytest.raises(YouTubeSourceError, match="does not match the requested interval"):
        source._trim_source_audio(original, stage, 20_000, 30_000, runner)


def test_encoded_mp3_is_checked_against_requested_duration_not_a_short_clip(tmp_path: Path):
    from yoto_mcp import youtube_source as source

    mp3 = tmp_path / "clip.mp3"

    def runner(args, **kwargs):
        if Path(args[0]).name == "ffprobe":
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({
                "streams": [{"codec_name": "mp3"}], "format": {"duration": "9.6"},
            }), stderr="")
        if Path(args[0]).name == "ffmpeg" and "volumedetect" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="", stderr="mean_volume: -20.0 dB\nmax_volume: -1.0 dB",
            )
        raise AssertionError("unexpected subprocess after a short encoded clip")

    with pytest.raises(YouTubeSourceError, match="does not match the requested source clip"):
        source._verify_mp3(mp3, runner, expected_duration_ms=10_000)


@pytest.mark.skipif(
    not Path("/usr/bin/ffmpeg").is_file() or not Path("/usr/bin/ffprobe").is_file(),
    reason="real FFmpeg/ffprobe integration binaries are unavailable",
)
def test_real_ffmpeg_decodes_and_validates_a_one_second_source_clip(tmp_path: Path):
    from yoto_mcp import youtube_source as source

    original = tmp_path / "source.wav"
    generated = subprocess.run(
        [
            "/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-ac", "2",
            "-c:a", "pcm_s16le", str(original),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert generated.returncode == 0

    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    clipped, actual_start, actual_end, duration_ms = source._trim_source_audio(
        original, stage, 1500, 2500, subprocess.run,
    )
    mp3 = stage / "clip.mp3"
    source._encode_mp3(clipped, mp3, "Test Artist", "Short Clip", "", subprocess.run)
    source._verify_mp3(mp3, subprocess.run, expected_duration_ms=duration_ms)

    assert clipped.is_file()
    assert (actual_start, actual_end, duration_ms) == (1500, 2500, 1000)
    assert mp3.stat().st_size >= 128
    assert original.is_file()

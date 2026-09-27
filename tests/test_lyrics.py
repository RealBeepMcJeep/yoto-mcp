from __future__ import annotations

import httpx
import pytest

from yoto_mcp.lyrics import LyricLookupError, lookup_lyric_evidence


def test_matched_lookup_returns_provider_fields_and_sends_identifying_user_agent():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/get"
        assert request.url.params["artist_name"] == "Example Artist"
        assert request.url.params["track_name"] == "Example Title"
        assert request.url.params["album_name"] == "Example Album"
        assert request.url.params["duration"] == "180"
        return httpx.Response(200, json={
            "id": 1, "trackName": "Example Title", "artistName": "Example Artist",
            "albumName": "Example Album", "duration": 180, "instrumental": False,
            "plainLyrics": "placeholder line one\nplaceholder line two",
            "syncedLyrics": "[00:01.00] placeholder line one",
        })

    result = lookup_lyric_evidence(
        "Example Artist", "Example Title", album="Example Album",
        duration_seconds=180, transport=httpx.MockTransport(handler),
    )
    assert result == {
        "status": "matched", "name": "lrclib", "source_id": 1,
        "source_url": "https://lrclib.net/",
        "track_name": "Example Title", "artist_name": "Example Artist",
        "album_name": "Example Album", "duration": 180,
        "plain_lyrics": "placeholder line one\nplaceholder line two",
        "synced_lyrics": "[00:01.00] placeholder line one",
    }
    assert requests[0].headers["user-agent"].startswith("yoto-mcp/")


def test_no_match_is_a_status_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": 404, "name": "TrackNotFound", "message": "not found"})

    result = lookup_lyric_evidence("Nobody", "Nothing", transport=httpx.MockTransport(handler))
    assert result == {"status": "no_match"}


def test_instrumental_track_is_reported_without_lyric_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 7, "instrumental": True})

    result = lookup_lyric_evidence("Artist", "Title", transport=httpx.MockTransport(handler))
    assert result == {"status": "instrumental", "source_id": 7, "source_url": "https://lrclib.net/"}


def test_rate_limit_honors_retry_after_without_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"}, json={"code": 429, "name": "TooManyRequests"})

    result = lookup_lyric_evidence("Artist", "Title", transport=httpx.MockTransport(handler))
    assert result == {"status": "provider_unavailable", "retry_after": 30}


def test_server_error_is_provider_unavailable_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    result = lookup_lyric_evidence("Artist", "Title", transport=httpx.MockTransport(handler))
    assert result == {"status": "provider_unavailable"}


def test_malformed_json_raises_sanitized_error_without_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})

    with pytest.raises(LyricLookupError, match="invalid response"):
        lookup_lyric_evidence("Artist", "Title", transport=httpx.MockTransport(handler))


def test_network_failure_raises_sanitized_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(LyricLookupError, match="request failed"):
        lookup_lyric_evidence("Artist", "Title", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("artist", "title", "album", "duration"),
    [
        ("", "Title", None, None),
        ("Artist", "", None, None),
        ("Artist", "Title", "", None),
        ("Artist", "Title", None, 0),
        ("Artist", "Title", None, 3601),
        ("Artist", "Title", None, True),
        ("Artist", "Title", None, 1.5),
        ("Control\x00Char", "Title", None, None),
        ("A" * 201, "Title", None, None),
    ],
)
def test_invalid_inputs_are_rejected_before_any_network_call(artist, title, album, duration):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid input must not reach the network")

    with pytest.raises(ValueError):
        lookup_lyric_evidence(
            artist, title, album=album, duration_seconds=duration,
            transport=httpx.MockTransport(handler),
        )

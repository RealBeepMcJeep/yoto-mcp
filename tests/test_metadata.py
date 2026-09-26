from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from yoto_mcp.metadata import format_track_title, lookup_recordings, read_mp3_tags


def _syncsafe(value: int) -> bytes:
    return bytes(((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F))


def _id3_tag(version: int, frames: list[tuple[str, bytes]]) -> bytes:
    body = bytearray()
    for frame_id, payload in frames:
        size = _syncsafe(len(payload)) if version == 4 else len(payload).to_bytes(4, "big")
        body.extend(frame_id.encode("ascii") + size + b"\x00\x00" + payload)
    return b"ID3" + bytes((version, 0, 0)) + _syncsafe(len(body)) + bytes(body)


def _text(value: str, encoding: int = 0) -> bytes:
    codec = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}[encoding]
    return bytes((encoding,)) + value.encode(codec)


def test_reads_id3v23_latin1_artist_title_and_album(tmp_path: Path):
    path = tmp_path / "song.mp3"
    path.write_bytes(
        _id3_tag(
            3,
            [
                ("TPE1", _text("Beyoncé")),
                ("TIT2", _text("Déjà Vu")),
                ("TALB", _text("B'Day")),
            ],
        )
        + b"audio frames"
    )

    assert read_mp3_tags(path) == {"artist": "Beyoncé", "title": "Déjà Vu", "album": "B'Day"}


def test_reads_id3v24_utf8_and_utf16_text(tmp_path: Path):
    path = tmp_path / "unicode.mp3"
    path.write_bytes(
        _id3_tag(
            4,
            [
                ("TPE1", _text("東京事変", encoding=3)),
                ("TIT2", _text("音楽", encoding=1)),
                ("TALB", _text("作品集", encoding=2)),
            ],
        )
    )

    assert read_mp3_tags(path) == {"artist": "東京事変", "title": "音楽", "album": "作品集"}


def test_reads_id3v23_utf16_text_with_bom(tmp_path: Path):
    path = tmp_path / "unicode-v23.mp3"
    path.write_bytes(_id3_tag(3, [("TIT2", _text("心を開いて", encoding=1))]))

    assert read_mp3_tags(path)["title"] == "心を開いて"


def test_rejects_invalid_id3_utf8_instead_of_returning_partial_tags(tmp_path: Path):
    path = tmp_path / "broken.mp3"
    path.write_bytes(_id3_tag(4, [("TPE1", _text("valid", encoding=3)), ("TIT2", b"\x03\xff")]))

    with pytest.raises(ValueError, match="Malformed ID3 text frame"):
        read_mp3_tags(path)


def test_reads_id3v23_global_unsynchronization_across_unknown_frames(tmp_path: Path):
    path = tmp_path / "unsynchronized.mp3"
    original_payload = b"owner\x00\xff\xe0private"
    stored_payload = original_payload.replace(b"\xff\xe0", b"\xff\x00\xe0")
    first_frame = b"PRIV" + len(original_payload).to_bytes(4, "big") + b"\x00\x00" + stored_payload
    title = _text("After unsynchronization")
    title_frame = b"TIT2" + len(title).to_bytes(4, "big") + b"\x00\x00" + title
    body = first_frame + title_frame
    path.write_bytes(b"ID3\x03\x00\x80" + _syncsafe(len(body)) + body)

    assert read_mp3_tags(path)["title"] == "After unsynchronization"


def test_rejects_id3_tags_over_the_configured_size_limit(tmp_path: Path):
    path = tmp_path / "oversized.mp3"
    size = 16 * 1024 * 1024 + 1
    path.write_bytes(b"ID3\x04\x00\x00" + _syncsafe(size))

    with pytest.raises(ValueError, match="ID3 tag exceeds size limit"):
        read_mp3_tags(path)


def test_mp3_without_id3v2_returns_empty_metadata(tmp_path: Path):
    path = tmp_path / "untagged.mp3"
    path.write_bytes(b"audio data")

    assert read_mp3_tags(path) == {"artist": "", "title": "", "album": ""}


@pytest.mark.parametrize(
    ("artist", "title", "expected"),
    [
        ("Artist", "Title", "Artist — Title"),
        ("Artist", "", "Artist"),
        ("", "Title", "Title"),
        (None, None, "Untitled"),
    ],
)
def test_formats_track_title_with_missing_field_fallback(artist, title, expected):
    assert format_track_title(artist, title) == expected


def test_lookup_recordings_returns_external_candidates_and_uses_safe_get_request():
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "recordings": [
                    {
                        "id": "recording-1",
                        "title": "Dead Man's Party",
                        "score": 97,
                        "artist-credit": [
                            {"artist": {"name": "Oingo Boingo"}, "joinphrase": " & "},
                            {"artist": {"name": "Example Artist"}},
                        ],
                        "releases": [{"title": "Dead Man's Party"}],
                    }
                ]
            },
        )

    candidates = lookup_recordings("Dead Man's Party", httpx.MockTransport(respond))

    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/ws/2/recording/"
    assert seen[0].url.params["query"] == "Dead Man's Party"
    assert seen[0].url.params["fmt"] == "json"
    assert seen[0].url.params["limit"] == "10"
    assert seen[0].headers["user-agent"].startswith("yoto-mcp/")
    assert "authorization" not in seen[0].headers
    assert candidates == [
        {
            "id": "recording-1",
            "title": "Dead Man's Party",
            "artist": "Oingo Boingo & Example Artist",
            "score": 97,
            "release": "Dead Man's Party",
        }
    ]


def test_lookup_recordings_caps_candidate_results_at_ten():
    recordings = [
        {"id": f"recording-{index}", "title": f"Track {index}", "score": 50}
        for index in range(13)
    ]
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"recordings": recordings}))

    candidates = lookup_recordings("bounded search", transport)

    assert len(candidates) == 10
    assert candidates[0]["id"] == "recording-0"
    assert candidates[-1]["id"] == "recording-9"


def test_lookup_errors_do_not_echo_query_or_upstream_details():
    query = "private-search-term"

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream-private-detail")

    with pytest.raises(ValueError) as error:
        lookup_recordings(query, httpx.MockTransport(respond))

    assert str(error.value) == "MusicBrainz lookup failed"
    assert query not in str(error.value)
    assert "upstream-private-detail" not in str(error.value)


def test_lookup_skips_out_of_range_scores_in_malformed_candidate_data():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={"recordings": [{"id": "oversized-score", "title": "Track", "score": 10**500}]},
        )
    )

    assert lookup_recordings("score boundary", transport) == []

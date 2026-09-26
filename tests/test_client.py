from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from yoto_mcp.media import file_sha256
from yoto_mcp.yoto import YotoAPIError, YotoClient

CARD = {
    "cardId": "card-1",
    "title": "Example Playlist 314",
    "slug": "example-playlist-314",
    "userId": "family-1",
    "updatedAt": "2026-01-01T00:00:00Z",
    "unknownCardField": {"keep": True},
    "metadata": {"media": {"duration": 9, "fileSize": 512}},
    "content": {
        "chapters": [
            {
                "key": "chapter-1",
                "title": "Chapter one",
                "unknownChapterField": "keep too",
                "tracks": [
                    {
                        "key": "track-1",
                        "title": "Existing song",
                        "trackUrl": "yoto:#" + "a" * 43,
                        "type": "audio",
                        "duration": 9,
                        "fileSize": 512,
                    }
                ],
            }
        ]
    },
}


@pytest.fixture

def fake_yoto():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/content/mine":
            return httpx.Response(200, json={"cards": [copy.deepcopy(CARD)]}, request=request)
        if request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(CARD)}, request=request)
        return httpx.Response(404, json={"error": "not found"}, request=request)

    return requests, httpx.MockTransport(handler)


def test_list_playlists_returns_compact_card_summary_and_auth(fake_yoto):
    requests, transport = fake_yoto
    client = YotoClient(lambda: "test-token", transport=transport)

    result = client.list_playlists()

    assert result == [
        {
            "cardId": "card-1",
            "title": "Example Playlist 314",
            "slug": "example-playlist-314",
            "updatedAt": "2026-01-01T00:00:00Z",
            "track_count": 1,
            "pending_tracks": [],
        }
    ]
    assert requests[0].url.path == "/content/mine"
    assert requests[0].headers["authorization"] == "Bearer test-token"


def test_list_playlists_fetches_details_when_summary_omits_chapters():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/content/mine":
            summary = {**CARD, "content": {"config": {}, "playbackType": "linear"}}
            return httpx.Response(200, json={"cards": [summary]}, request=request)
        if request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(CARD)}, request=request)
        return httpx.Response(404, request=request)

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler))
    result = client.list_playlists()

    assert result[0]["track_count"] == 1
    assert [request.url.path for request in requests] == ["/content/mine", "/content/card-1"]


def test_get_playlist_returns_full_card_without_losing_unknown_fields(fake_yoto):
    requests, transport = fake_yoto
    client = YotoClient(lambda: "test-token", transport=transport)

    result = client.get_playlist("card-1")

    assert result == CARD
    assert result["unknownCardField"] == {"keep": True}
    assert result["content"]["chapters"][0]["unknownChapterField"] == "keep too"
    assert requests[0].url.path == "/content/card-1"
    assert requests[0].headers["authorization"] == "Bearer test-token"


def test_get_playlist_requires_card_id(fake_yoto):
    _, transport = fake_yoto
    client = YotoClient(lambda: "test-token", transport=transport)

    with pytest.raises(ValueError, match="card id"):
        client.get_playlist(" ")


def test_add_mp3_defaults_to_dry_run_without_upload(tmp_path, fake_yoto):
    requests, transport = fake_yoto
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    song = upload_root / "new-song.mp3"
    song.write_bytes(b"ID3" + b"x" * 300)
    client = YotoClient(lambda: "test-token", transport=transport, upload_root=upload_root)

    result = client.add_mp3("card-1", "chapter-1", "new-song.mp3")

    assert result == {
        "dry_run": True,
        "action": "add_mp3",
        "cardId": "card-1",
        "chapter_key": "chapter-1",
        "file": "new-song.mp3",
        "proposed_title": "new-song",
    }
    assert [request.url.path for request in requests] == ["/content/card-1"]


def test_add_mp3_upload_transcode_append_and_read_back(tmp_path):
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    song = upload_root / "new-song.mp3"
    song.write_bytes(b"ID3" + b"audio" * 100)
    requests: list[httpx.Request] = []
    posted_cards: list[dict] = []
    current_card = copy.deepcopy(CARD)
    poll_count = 0
    media_id = "b" * 43

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal current_card, poll_count
        requests.append(request)
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(current_card)}, request=request)
        if request.method == "GET" and request.url.path == "/media/transcode/audio/uploadUrl":
            return httpx.Response(
                200,
                json={"upload": {"uploadUrl": "https://yoto-media-api-prod-uploads.s3.eu-west-2.amazonaws.com/upload-slot", "uploadId": "upload-1"}},
                request=request,
            )
        if request.method == "PUT" and request.url.path == "/upload-slot":
            return httpx.Response(200, request=request)
        if request.method == "GET" and request.url.path == "/media/upload/upload-1/transcoded":
            poll_count += 1
            if poll_count == 1:
                return httpx.Response(
                    200,
                    json={"transcode": {"startedAt": "pending"}},
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "transcode": {
                        "transcodedSha256": media_id,
                        "transcodedInfo": {
                            "duration": 12.8,
                            "channels": "stereo",
                            "format": "mp3",
                            "fileSize": 888,
                            "metadata": {"title": "Encoded song"},
                        },
                    }
                },
                request=request,
            )
        if request.method == "POST" and request.url.path == "/content":
            current_card = json.loads(request.content)
            posted_cards.append(copy.deepcopy(current_card))
            return httpx.Response(204, request=request)
        return httpx.Response(404, json={"error": "not found"}, request=request)

    client = YotoClient(
        lambda: "test-token",
        transport=httpx.MockTransport(handler),
        upload_root=upload_root,
        allow_writes=True,
        dry_run=False,
        poll_interval=0,
    )

    journal_events = []

    def reserved(track_key, chapter_key):
        journal_events.append(("reserved", track_key, chapter_key, [r.method for r in requests]))

    def media_journaled(media_hash):
        journal_events.append(("media_hash", media_hash, [r.method for r in requests]))

    result = client.add_mp3(
        "card-1", "chapter-1", "new-song.mp3",
        on_reserved=reserved,
        on_media_hash=media_journaled,
    )

    assert [event[0] for event in journal_events] == ["reserved", "media_hash"]
    assert journal_events[0][3] == ["GET"]
    assert "POST" not in journal_events[1][2]
    assert [request.method for request in requests] == [
        "GET",
        "GET",
        "PUT",
        "GET",
        "GET",
        "GET",
        "POST",
        "GET",
    ]
    assert [request.url.path for request in requests] == [
        "/content/card-1",
        "/media/transcode/audio/uploadUrl",
        "/upload-slot",
        "/media/upload/upload-1/transcoded",
        "/media/upload/upload-1/transcoded",
        "/content/card-1",
        "/content",
        "/content/card-1",
    ]
    upload_request = requests[1]
    assert upload_request.url.params["sha256"] == file_sha256(song)
    assert upload_request.url.params["filename"] == "new-song.mp3"
    assert upload_request.url.params["mediaAccount"] == CARD["userId"]
    assert requests[3].url.params["loudnorm"] == "false"
    assert requests[4].url.params["loudnorm"] == "false"
    assert requests[2].headers["content-disposition"] == 'attachment; filename="new-song.mp3"'
    assert "authorization" not in requests[2].headers
    assert requests[2].content == song.read_bytes()

    saved = posted_cards[0]
    assert saved["unknownCardField"] == CARD["unknownCardField"]
    saved_chapter = saved["content"]["chapters"][0]
    assert saved_chapter["unknownChapterField"] == "keep too"
    assert saved_chapter["tracks"][0] == CARD["content"]["chapters"][0]["tracks"][0]
    appended = saved_chapter["tracks"][1]
    assert journal_events[0][1:3] == (appended["key"], "chapter-1")
    assert journal_events[1][1] == media_id
    assert appended == {
        "key": appended["key"],
        "title": "Encoded song",
        "trackUrl": f"yoto:#{media_id}",
        "type": "audio",
        "duration": 12,
        "channels": "stereo",
        "format": "mp3",
        "fileSize": 888,
    }
    assert len(appended["key"]) <= 20
    assert saved["metadata"]["media"] == {"duration": 21, "fileSize": 1400}
    assert result["content"]["chapters"][0]["tracks"][-1] == appended
    assert "upload-slot" not in repr(result)


def test_add_mp3_upload_header_folds_non_ascii_filename(tmp_path):
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    filename = "12_Médio grave & Jb no beat.mp3"
    song = upload_root / filename
    song.write_bytes(b"ID3" + b"audio" * 100)
    requests: list[httpx.Request] = []
    current_card = copy.deepcopy(CARD)
    media_id = "c" * 43

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal current_card
        requests.append(request)
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(current_card)}, request=request)
        if request.method == "GET" and request.url.path == "/media/transcode/audio/uploadUrl":
            return httpx.Response(
                200,
                json={"upload": {"uploadUrl": "https://yoto-media-api-prod-uploads.s3.eu-west-2.amazonaws.com/upload-slot", "uploadId": "upload-1"}},
                request=request,
            )
        if request.method == "PUT" and request.url.path == "/upload-slot":
            return httpx.Response(200, request=request)
        if request.method == "GET" and request.url.path == "/media/upload/upload-1/transcoded":
            return httpx.Response(
                200,
                json={
                    "transcode": {
                        "transcodedSha256": media_id,
                        "transcodedInfo": {"duration": 12.8, "fileSize": 888, "metadata": {"title": "Encoded song"}},
                    }
                },
                request=request,
            )
        if request.method == "POST" and request.url.path == "/content":
            current_card = json.loads(request.content)
            return httpx.Response(204, request=request)
        return httpx.Response(404, json={"error": "not found"}, request=request)

    client = YotoClient(
        lambda: "test-token",
        transport=httpx.MockTransport(handler),
        upload_root=upload_root,
        allow_writes=True,
        dry_run=False,
        poll_interval=0,
    )

    client.add_mp3("card-1", "chapter-1", filename)

    upload_request = next(request for request in requests if request.method == "PUT")
    disposition = upload_request.headers["content-disposition"]
    assert disposition == 'attachment; filename="12_Medio grave & Jb no beat.mp3"'
    # httpx raises UnicodeEncodeError when sending non-ASCII header values.
    assert disposition.isascii()
    upload_params = next(request for request in requests if request.url.path == "/media/transcode/audio/uploadUrl")
    assert upload_params.url.params["filename"] == filename


def test_add_mp3_new_chapter_matches_frontend_one_song_per_chapter(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    def frame(name: str, text: str) -> bytes:
        payload = b"\x03" + text.encode("utf-8")
        return name.encode() + bytes((0, 0, 0, len(payload))) + b"\x00\x00" + payload

    id3 = frame("TPE1", "Lenka") + frame("TIT2", "Everything at Once")
    (root / "Lenka - Everything at Once.mp3").write_bytes(
        b"ID3\x04\x00\x00" + bytes((0, 0, 0, len(id3))) + id3 + b"\xff\xfb" + b"x" * 300
    )
    card = copy.deepcopy(CARD)
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal card
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(card)}, request=request)
        if request.url.path == "/media/transcode/audio/uploadUrl":
            return httpx.Response(200, json={"upload": {
                "uploadUrl": "https://yoto-media-api-prod-uploads.s3.eu-west-2.amazonaws.com/slot",
                "uploadId": "up-1",
            }}, request=request)
        if request.method == "PUT" and request.url.path == "/slot":
            return httpx.Response(200, request=request)
        if request.url.path == "/media/upload/up-1/transcoded":
            return httpx.Response(200, json={"transcode": {
                "transcodedSha256": "b" * 43,
                "transcodedInfo": {"duration": 158.4, "fileSize": 1000, "format": "mp3",
                                   "channels": "stereo", "metadata": {"title": "Everything at Once"}},
            }}, request=request)
        if request.method == "POST" and request.url.path == "/content":
            card = json.loads(request.content)
            posted.append(copy.deepcopy(card))
            return httpx.Response(200, json={"card": card}, request=request)
        raise AssertionError("Unexpected request")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler),
                        upload_root=root, allow_writes=True, dry_run=False)
    reserved_keys = []
    result = client.add_mp3(
        "card-1", "new", "Lenka - Everything at Once.mp3",
        on_reserved=lambda track_key, chapter_key: reserved_keys.append((track_key, chapter_key)),
    )

    assert len(posted) == 1
    chapters = posted[0]["content"]["chapters"]
    assert chapters[0] == CARD["content"]["chapters"][0]
    assert len(chapters) == 2
    assert chapters[1]["key"] != "chapter-1"
    assert chapters[1]["title"] == "Lenka — Everything at Once"
    assert chapters[1]["tracks"][0]["title"] == "Lenka — Everything at Once"
    assert chapters[1]["tracks"][0]["trackUrl"] == "yoto:#" + "b" * 43
    assert reserved_keys == [(chapters[1]["tracks"][0]["key"], chapters[1]["key"])]
    assert result["content"]["chapters"][-1] == chapters[-1]


@pytest.mark.parametrize(("chapter_key", "pending_chapter_index"), [("chapter-1", 0), ("new", 1)])
def test_transcoding_status_is_visible_while_upload_is_pending(tmp_path, chapter_key, pending_chapter_index):
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    (upload_root / "pending.mp3").write_bytes(b"ID3" + b"x" * 300)
    polling = threading.Event()
    continue_transcode = threading.Event()
    current_card = copy.deepcopy(CARD)
    media_id = "c" * 43

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal current_card
        if request.method == "GET" and request.url.path == "/content/mine":
            return httpx.Response(200, json={"cards": [copy.deepcopy(current_card)]}, request=request)
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(current_card)}, request=request)
        if request.url.path == "/media/transcode/audio/uploadUrl":
            return httpx.Response(
                200,
                json={"upload": {"uploadUrl": "https://yoto-media-api-prod-uploads.s3.eu-west-2.amazonaws.com/slot", "uploadId": "pending-1"}},
                request=request,
            )
        if request.method == "PUT":
            return httpx.Response(200, request=request)
        if request.url.path == "/media/upload/pending-1/transcoded":
            polling.set()
            assert continue_transcode.wait(timeout=3)
            return httpx.Response(
                200,
                json={"transcode": {"transcodedSha256": media_id, "transcodedInfo": {"duration": 1, "fileSize": 100}}},
                request=request,
            )
        if request.method == "POST" and request.url.path == "/content":
            current_card = json.loads(request.content)
            return httpx.Response(200, json={"card": copy.deepcopy(current_card)}, request=request)
        return httpx.Response(404, request=request)

    client = YotoClient(
        lambda: "test-token",
        transport=httpx.MockTransport(handler),
        upload_root=upload_root,
        allow_writes=True,
        dry_run=False,
        poll_interval=0,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        operation = executor.submit(client.add_mp3, "card-1", chapter_key, "pending.mp3")
        try:
            assert polling.wait(timeout=3)
            listing = client.list_playlists()
            playlist = client.get_playlist("card-1")
            assert listing[0]["pending_tracks"][0]["status"] == "transcoding"
            pending_track = playlist["content"]["chapters"][pending_chapter_index]["tracks"][-1]
            assert pending_track["_status"]["status"] == "transcoding"
        finally:
            continue_transcode.set()
        operation.result(timeout=3)

    assert client.list_playlists()[0]["pending_tracks"] == []


def test_add_mp3_requires_write_gate_before_upstream_calls(tmp_path, fake_yoto):
    requests, transport = fake_yoto
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    (upload_root / "blocked.mp3").write_bytes(b"ID3" + b"x" * 300)
    client = YotoClient(lambda: "test-token", transport=transport, upload_root=upload_root)

    with pytest.raises(PermissionError, match="YOTO_ALLOW_WRITES=1"):
        client.add_mp3("card-1", "chapter-1", "blocked.mp3", dry_run=False)

    assert requests == []


def test_remove_track_posts_full_preserved_card_and_reads_it_back():
    initial = copy.deepcopy(CARD)
    initial["content"]["chapters"][0]["tracks"].append(
        {
            "key": "track-10",
            "title": "Keep this song",
            "trackUrl": "yoto:#" + "d" * 43,
            "type": "audio",
            "duration": 4,
            "fileSize": 512,
        }
    )
    requests: list[httpx.Request] = []
    posted_cards: list[dict] = []
    current_card = initial

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal current_card
        requests.append(request)
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(current_card)}, request=request)
        if request.method == "POST" and request.url.path == "/content":
            current_card = json.loads(request.content)
            posted_cards.append(copy.deepcopy(current_card))
            return httpx.Response(204, request=request)
        return httpx.Response(404, request=request)

    client = YotoClient(
        lambda: "test-token",
        transport=httpx.MockTransport(handler),
        allow_writes=True,
        dry_run=False,
    )

    result = client.remove_track("card-1", "track-1")

    assert [request.method for request in requests] == ["GET", "POST", "GET"]
    assert [request.url.path for request in requests] == ["/content/card-1", "/content", "/content/card-1"]
    saved = posted_cards[0]
    assert saved["unknownCardField"] == CARD["unknownCardField"]
    saved_chapter = saved["content"]["chapters"][0]
    assert saved_chapter["unknownChapterField"] == "keep too"
    assert [track["key"] for track in saved_chapter["tracks"]] == ["track-10"]
    assert saved["metadata"]["media"] == {"duration": 4, "fileSize": 512}
    assert result == saved


def test_rename_track_dry_run_previews_artist_title_and_preserves_card():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(CARD)}, request=request)
        raise AssertionError("Dry run must not write")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler))
    preview = client.rename_track("card-1", "track-1", "Example Artist", "Example Song")

    assert preview == {
        "dry_run": True, "cardId": "card-1", "track_key": "track-1",
        "old_title": "Existing song", "new_title": "Example Artist — Example Song",
        "old_chapter_title": "Chapter one", "new_chapter_title": "Example Artist — Example Song",
    }
    assert [r.method for r in requests] == ["GET"]


def test_rename_track_changes_only_exact_track_and_single_song_chapter():
    card = copy.deepcopy(CARD)
    card["content"]["chapters"].append({"key": "other", "title": "Keep", "tracks": [{
        "key": "other-track", "title": "Keep", "duration": 3, "fileSize": 333,
    }]})
    original_other = copy.deepcopy(card["content"]["chapters"][1])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal card
        requests.append(request)
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(card)}, request=request)
        if request.method == "POST" and request.url.path == "/content":
            card = json.loads(request.content)
            return httpx.Response(200, json={"card": card}, request=request)
        raise AssertionError("Unexpected request")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler),
                        allow_writes=True)
    result = client.rename_track("card-1", "track-1", "Example Artist", "Example Song", dry_run=False)
    assert [r.method for r in requests] == ["GET", "POST", "GET"]
    chapter = result["content"]["chapters"][0]
    assert chapter["title"] == "Example Artist — Example Song"
    assert chapter["tracks"][0]["title"] == "Example Artist — Example Song"
    assert result["content"]["chapters"][1] == original_other
    assert result["unknownCardField"] == CARD["unknownCardField"]


def test_invalid_transcode_metadata_fails_without_post_or_pending_status(tmp_path):
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    (upload_root / "bad-info.mp3").write_bytes(b"ID3" + b"x" * 300)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(CARD)}, request=request)
        if request.method == "GET" and request.url.path == "/content/mine":
            return httpx.Response(200, json={"cards": [copy.deepcopy(CARD)]}, request=request)
        if request.url.path == "/media/transcode/audio/uploadUrl":
            return httpx.Response(
                200,
                json={"upload": {"uploadUrl": "https://yoto-media-api-prod-uploads.s3.eu-west-2.amazonaws.com/slot", "uploadId": "bad-info"}},
                request=request,
            )
        if request.method == "PUT":
            return httpx.Response(200, request=request)
        if request.url.path == "/media/upload/bad-info/transcoded":
            return httpx.Response(
                200,
                json={"transcode": {"transcodedSha256": "e" * 43, "transcodedInfo": {"duration": 3}}},
                request=request,
            )
        return httpx.Response(404, request=request)

    client = YotoClient(
        lambda: "test-token",
        transport=httpx.MockTransport(handler),
        upload_root=upload_root,
        allow_writes=True,
        dry_run=False,
        poll_interval=0,
    )

    with pytest.raises(YotoAPIError, match="incomplete transcode metadata"):
        client.add_mp3("card-1", "chapter-1", "bad-info.mp3")

    assert all(request.url.path != "/content" for request in requests)
    assert client.list_playlists()[0]["pending_tracks"] == []


def test_malformed_presigned_url_is_sanitized(tmp_path):
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    (upload_root / "malformed.mp3").write_bytes(b"ID3" + b"x" * 300)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(CARD)}, request=request)
        if request.method == "GET" and request.url.path == "/content/mine":
            return httpx.Response(200, json={"cards": [copy.deepcopy(CARD)]}, request=request)
        if request.url.path == "/media/transcode/audio/uploadUrl":
            return httpx.Response(
                200,
                json={
                    "upload": {
                        "uploadUrl": "https://put.invalid:bad/path",
                        "uploadId": "malformed-url",
                    }
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    client = YotoClient(
        lambda: "test-token",
        transport=httpx.MockTransport(handler),
        upload_root=upload_root,
        allow_writes=True,
        dry_run=False,
    )

    with pytest.raises(YotoAPIError, match="invalid upload URL") as error:
        client.add_mp3("card-1", "chapter-1", "malformed.mp3")

    assert "put.invalid" not in str(error.value)
    assert client.list_playlists()[0]["pending_tracks"] == []


def test_upload_refuses_untrusted_signed_host_before_sending_mp3(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "song.mp3").write_bytes(b"ID3" + b"x" * 300)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(CARD)}, request=request)
        if request.url.path == "/media/transcode/audio/uploadUrl":
            return httpx.Response(200, json={"upload": {
                "uploadUrl": "https://untrusted.invalid/slot?signature=sensitive",
                "uploadId": "slot-1",
            }}, request=request)
        raise AssertionError("MP3 must not be sent to an untrusted host")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler),
                        upload_root=root, allow_writes=True, dry_run=False)
    with pytest.raises(YotoAPIError, match="invalid upload URL") as error:
        client.add_mp3("card-1", "chapter-1", "song.mp3")
    assert "sensitive" not in str(error.value)
    assert [r.url.path for r in requests] == ["/content/card-1", "/media/transcode/audio/uploadUrl"]


def test_upload_icon_dry_run_previews_without_network(tmp_path):
    root = tmp_path / "icons"
    root.mkdir()
    (root / "cat.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Dry run must not call the network")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler), upload_root=root)
    preview = client.upload_icon("cat.png")
    assert preview == {
        "dry_run": True, "action": "upload_icon",
        "file": "cat.png", "mime_type": "image/png", "auto_convert": True,
    }


def test_upload_icon_posts_raw_bytes_with_autoconvert_and_returns_media_id(tmp_path):
    root = tmp_path / "icons"
    root.mkdir()
    image = root / "cat.png"
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    image.write_bytes(image_bytes)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/media/displayIcons/user/me/upload"
        assert request.url.params["autoConvert"] == "true"
        assert request.headers["content-type"] == "image/png"
        assert request.content == image_bytes
        return httpx.Response(200, json={"displayIcon": {
            "mediaId": "abc123", "url": "https://media-secure.aws.com/icons/abc123", "new": True,
        }}, request=request)

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler),
                        upload_root=root, allow_writes=True, dry_run=False)
    result = client.upload_icon("cat.png")
    assert result == {"mediaId": "abc123", "url": "https://media-secure.aws.com/icons/abc123", "new": True}
    assert len(requests) == 1


def test_upload_icon_requires_write_gate_before_upstream_call(tmp_path):
    root = tmp_path / "icons"
    root.mkdir()
    (root / "cat.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Writes-disabled client must not call the network")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler), upload_root=root)
    with pytest.raises(PermissionError):
        client.upload_icon("cat.png", dry_run=False)


def test_set_track_icon_dry_run_previews_reference_and_preserves_card(fake_yoto):
    requests, transport = fake_yoto
    client = YotoClient(lambda: "test-token", transport=transport)
    preview = client.set_track_icon("card-1", "track-1", "abc123")
    assert preview == {
        "dry_run": True, "cardId": "card-1", "track_key": "track-1",
        "old_icon": None, "new_icon": "yoto:#abc123",
        "old_chapter_icon": None, "new_chapter_icon": "yoto:#abc123",
    }
    assert [r.method for r in requests] == ["GET"]


def test_set_track_icon_changes_only_exact_track_and_single_track_chapter():
    card = copy.deepcopy(CARD)
    card["content"]["chapters"].append({"key": "other", "title": "Keep", "tracks": [{
        "key": "other-track", "title": "Keep", "duration": 3, "fileSize": 333,
    }]})
    original_other = copy.deepcopy(card["content"]["chapters"][1])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal card
        requests.append(request)
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(card)}, request=request)
        if request.method == "POST" and request.url.path == "/content":
            card = json.loads(request.content)
            return httpx.Response(200, json={"card": card}, request=request)
        raise AssertionError("Unexpected request")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler), allow_writes=True)
    result = client.set_track_icon("card-1", "track-1", "abc123", dry_run=False)
    assert [r.method for r in requests] == ["GET", "POST", "GET"]
    chapter = result["content"]["chapters"][0]
    assert chapter["display"]["icon16x16"] == "yoto:#abc123"
    assert chapter["tracks"][0]["display"]["icon16x16"] == "yoto:#abc123"
    assert result["content"]["chapters"][1] == original_other
    assert result["unknownCardField"] == CARD["unknownCardField"]


def test_set_track_icon_rejects_malformed_media_id():
    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(AssertionError("must not call the network"))
    ))
    with pytest.raises(ValueError, match="format"):
        client.set_track_icon("card-1", "track-1", "not a valid id!")


def _two_chapter_card() -> dict:
    card = copy.deepcopy(CARD)
    card["content"]["chapters"][0]["overlayLabel"] = "1"
    card["content"]["chapters"][0]["tracks"][0]["overlayLabel"] = "1"
    card["content"]["chapters"].append({
        "key": "chapter-2", "title": "Chapter two", "overlayLabel": "2",
        "tracks": [{"key": "track-2", "title": "Second song", "overlayLabel": "2",
                    "trackUrl": "yoto:#" + "e" * 43, "type": "audio", "duration": 5, "fileSize": 500}],
    })
    return card


def test_reorder_chapters_dry_run_previews_order_without_writing():
    card = _two_chapter_card()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(card)}, request=request)
        raise AssertionError("Dry run must not write")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler))
    preview = client.reorder_chapters("card-1", ["chapter-2", "chapter-1"])
    assert preview == {
        "dry_run": True, "cardId": "card-1",
        "old_order": [{"key": "chapter-1", "title": "Chapter one"}, {"key": "chapter-2", "title": "Chapter two"}],
        "new_order": [{"key": "chapter-2", "title": "Chapter two"}, {"key": "chapter-1", "title": "Chapter one"}],
    }


def test_reorder_chapters_swaps_array_order_and_renumbers_labels():
    card = _two_chapter_card()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal card
        requests.append(request)
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(card)}, request=request)
        if request.method == "POST" and request.url.path == "/content":
            card = json.loads(request.content)
            return httpx.Response(200, json={"card": card}, request=request)
        raise AssertionError("Unexpected request")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler), allow_writes=True)
    result = client.reorder_chapters("card-1", ["chapter-2", "chapter-1"], dry_run=False)
    assert [r.method for r in requests] == ["GET", "POST", "GET"]
    chapters = result["content"]["chapters"]
    assert [c["key"] for c in chapters] == ["chapter-2", "chapter-1"]
    assert chapters[0]["overlayLabel"] == "1" and chapters[0]["tracks"][0]["overlayLabel"] == "1"
    assert chapters[1]["overlayLabel"] == "2" and chapters[1]["tracks"][0]["overlayLabel"] == "2"


def test_reorder_chapters_rejects_incomplete_or_unknown_keys():
    card = _two_chapter_card()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(card)}, request=request)
        raise AssertionError("Invalid input must not reach the network")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="exactly once"):
        client.reorder_chapters("card-1", ["chapter-1"])
    with pytest.raises(ValueError, match="exactly once"):
        client.reorder_chapters("card-1", ["chapter-1", "chapter-99"])


def test_export_track_dry_run_previews_filename_without_network(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(CARD)}, request=request)
        raise AssertionError("Dry run must never mint a signed URL")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler), upload_root=root)
    preview = client.export_track("card-1", "track-1")
    assert preview["dry_run"] is True
    assert preview["proposed_file"] == "Existing song.mp3"
    assert "byte-identical" in preview["note"]


def test_export_track_downloads_signed_url_from_pinned_host_only(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    audio_bytes = b"fake-audio-bytes" * 10
    signed_url = "https://yoto-card-api-prod-media.s3.eu-west-2.amazonaws.com/signed-path?X-Amz-Signature=secret"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/content/card-1" and "playable" not in request.url.params:
            return httpx.Response(200, json={"card": copy.deepcopy(CARD)}, request=request)
        if request.method == "GET" and request.url.path == "/content/card-1" and request.url.params.get("playable") == "true":
            signed_card = copy.deepcopy(CARD)
            signed_card["content"]["chapters"][0]["tracks"][0]["trackUrl"] = signed_url
            return httpx.Response(200, json={"card": signed_card}, request=request)
        if str(request.url) == signed_url:
            return httpx.Response(200, content=audio_bytes, request=request)
        raise AssertionError(f"Unexpected request to {request.url}")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler),
                        upload_root=root, allow_writes=True)
    result = client.export_track("card-1", "track-1", dry_run=False)
    saved = root / "Existing song.mp3"
    assert saved.read_bytes() == audio_bytes
    assert result == {"file": str(saved), "title": "Existing song", "bytes": len(audio_bytes)}


def test_export_track_refuses_untrusted_signed_host(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/content/card-1" and "playable" not in request.url.params:
            return httpx.Response(200, json={"card": copy.deepcopy(CARD)}, request=request)
        if request.method == "GET" and request.url.path == "/content/card-1":
            signed_card = copy.deepcopy(CARD)
            signed_card["content"]["chapters"][0]["tracks"][0]["trackUrl"] = "https://untrusted.invalid/steal?sig=sensitive"
            return httpx.Response(200, json={"card": signed_card}, request=request)
        raise AssertionError("Must not fetch audio from an untrusted host")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler),
                        upload_root=root, allow_writes=True)
    with pytest.raises(YotoAPIError, match="unexpected host") as error:
        client.export_track("card-1", "track-1", dry_run=False)
    assert "sensitive" not in str(error.value)
    assert not (root / "Existing song.mp3").exists()


def test_export_track_refuses_to_overwrite_existing_file(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    (root / "Existing song.mp3").write_bytes(b"already here")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(CARD)}, request=request)
        raise AssertionError("Must not mint a signed URL when the destination already exists")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler),
                        upload_root=root, allow_writes=True)
    with pytest.raises(ValueError, match="already exists"):
        client.export_track("card-1", "track-1", dry_run=False)


def test_export_track_requires_write_gate_before_upstream_calls(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Writes-disabled client must not call the network")

    client = YotoClient(lambda: "test-token", transport=httpx.MockTransport(handler), upload_root=root)
    with pytest.raises(PermissionError):
        client.export_track("card-1", "track-1", dry_run=False)


def test_add_mp3_reserved_callback_exception_prevents_upload_or_card_write(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "song.mp3").write_bytes(b"ID3" + b"x" * 300)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(CARD)}, request=request)
        raise AssertionError("A failed reservation journal must prevent upload requests")

    def fail_reserved(_track_key, _chapter_key):
        raise RuntimeError("journal unavailable")

    client = YotoClient(
        lambda: "test-token",
        transport=httpx.MockTransport(handler),
        upload_root=root,
        allow_writes=True,
        dry_run=False,
    )

    with pytest.raises(RuntimeError, match="journal unavailable"):
        client.add_mp3("card-1", "chapter-1", "song.mp3", on_reserved=fail_reserved)

    assert [request.url.path for request in requests] == ["/content/card-1"]


def test_add_mp3_media_callback_exception_prevents_whole_card_post(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "song.mp3").write_bytes(b"ID3" + b"x" * 300)
    requests = []
    media_hash = "f" * 43

    def handler(request):
        requests.append(request)
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(CARD)}, request=request)
        if request.url.path == "/media/transcode/audio/uploadUrl":
            return httpx.Response(200, json={"upload": {
                "uploadUrl": "https://yoto-media-api-prod-uploads.s3.eu-west-2.amazonaws.com/slot",
                "uploadId": "callback-test",
            }}, request=request)
        if request.method == "PUT":
            return httpx.Response(200, request=request)
        if request.url.path == "/media/upload/callback-test/transcoded":
            return httpx.Response(200, json={"transcode": {
                "transcodedSha256": media_hash,
                "transcodedInfo": {"duration": 5, "fileSize": 100, "metadata": {"title": "Song"}},
            }}, request=request)
        raise AssertionError("A failed media journal must prevent the full-card POST")

    def fail_media(_media_hash):
        raise RuntimeError("media journal unavailable")

    client = YotoClient(
        lambda: "test-token",
        transport=httpx.MockTransport(handler),
        upload_root=root,
        allow_writes=True,
        dry_run=False,
        poll_interval=0,
    )

    with pytest.raises(RuntimeError, match="media journal unavailable"):
        client.add_mp3(
            "card-1", "chapter-1", "song.mp3",
            on_media_hash=fail_media,
        )

    assert any(request.method == "PUT" for request in requests)
    assert all(request.method != "POST" for request in requests)


@pytest.mark.parametrize(
    ("failure", "expected_operation", "expected_status"),
    [
        ("local_preflight", "local_preflight", None),
        ("upload_url", "upload_url", 503),
        ("upload_put", "upload_put", 502),
        ("transcode", "transcode", None),
    ],
)
def test_add_mp3_reports_safe_failure_boundary_and_http_status(
    tmp_path, failure, expected_operation, expected_status,
):
    root = tmp_path / "uploads"
    root.mkdir()
    song = root / "song.mp3"
    song.write_bytes(b"ID3" + b"x" * 300)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/content/card-1":
            return httpx.Response(200, json={"card": copy.deepcopy(CARD)}, request=request)
        if request.url.path == "/media/transcode/audio/uploadUrl":
            if failure == "upload_url":
                return httpx.Response(
                    503, text="response-body-secret https://signed.invalid/?sig=secret",
                    request=request,
                )
            return httpx.Response(200, json={"upload": {
                "uploadUrl": "https://yoto-media-api-prod-uploads.s3.eu-west-2.amazonaws.com/slot",
                "uploadId": "diagnostic-upload",
            }}, request=request)
        if request.method == "PUT":
            if failure == "upload_put":
                return httpx.Response(
                    502, text="upload body secret Bearer token-secret", request=request,
                )
            return httpx.Response(200, request=request)
        if request.url.path == "/media/upload/diagnostic-upload/transcoded":
            if failure == "transcode":
                return httpx.Response(200, json={"transcode": {"erroredAt": "secret"}}, request=request)
            raise AssertionError("The selected failure must stop before the transcode poll")
        raise AssertionError("Unexpected request")

    client = YotoClient(
        lambda: "token-secret", transport=httpx.MockTransport(handler),
        upload_root=root, allow_writes=True, dry_run=False, poll_interval=0,
    )
    failures = []

    with pytest.raises((YotoAPIError, ValueError)):
        client.add_mp3(
            "card-1", "chapter-1", "missing.mp3" if failure == "local_preflight" else "song.mp3",
            on_failure=lambda operation, exc: failures.append((operation, exc)),
        )

    assert len(failures) == 1
    operation, error = failures[0]
    assert operation == expected_operation
    assert getattr(error, "http_status", None) == expected_status
    assert all(secret not in str(error) for secret in (
        "response-body-secret", "signed.invalid", "Bearer token-secret", "token-secret",
    ))
    if failure == "local_preflight":
        assert requests == []
    if failure in {"upload_url", "upload_put"}:
        assert all(request.method != "POST" for request in requests)

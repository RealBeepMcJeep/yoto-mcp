from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from mcp.types import CallToolResult

from yoto_mcp.config import Settings
from yoto_mcp.server import create_server


class RecordingClient:
    def __init__(self):
        self.calls = []

    def list_playlists(self):
        self.calls.append(("list_playlists",))
        return [{"cardId": "card-1"}]

    def get_playlist(self, card_id):
        self.calls.append(("get_playlist", card_id))
        return {"cardId": card_id}

    def add_mp3(self, card_id, chapter_key, file_path, *, dry_run=True, title=None):
        self.calls.append(("add_mp3", card_id, chapter_key, file_path, dry_run, title))
        return {"dry_run": dry_run}

    def rename_track(self, card_id, track_key, artist, title, *, dry_run=True):
        self.calls.append(("rename_track", card_id, track_key, artist, title, dry_run))
        return {"dry_run": dry_run}

    def remove_track(self, card_id, track_key, *, dry_run=True):
        self.calls.append(("remove_track", card_id, track_key, dry_run))
        return {"dry_run": dry_run}

    def upload_icon(self, file_path, *, auto_convert=True, filename=None, dry_run=True):
        self.calls.append(("upload_icon", file_path, auto_convert, filename, dry_run))
        return {"dry_run": dry_run}

    def set_track_icon(self, card_id, track_key, media_id, *, dry_run=True):
        self.calls.append(("set_track_icon", card_id, track_key, media_id, dry_run))
        return {"dry_run": dry_run}

    def reorder_chapters(self, card_id, chapter_keys, *, dry_run=True):
        self.calls.append(("reorder_chapters", card_id, tuple(chapter_keys), dry_run))
        return {"dry_run": dry_run}

    def export_track(self, card_id, track_key, *, destination_name=None, dry_run=True):
        self.calls.append(("export_track", card_id, track_key, destination_name, dry_run))
        return {"dry_run": dry_run}


def test_server_registers_list_read_add_remove_tools_and_defaults_mutations_to_dry_run(tmp_path: Path):
    settings = Settings(client_id="test-client", upload_root=tmp_path, allow_writes=True)
    client = RecordingClient()
    received_settings = []

    def factory(given_settings):
        received_settings.append(given_settings)
        return client

    server = create_server(settings, client_factory=factory)

    tools = asyncio.run(server.list_tools())

    assert {tool.name for tool in tools} == {
        "list_playlists",
        "get_playlist",
        "add_mp3",
        "rename_track",
        "remove_track",
        "inspect_mp3_metadata",
        "lookup_recordings",
        "upload_icon",
        "set_track_icon",
        "reorder_chapters",
        "export_track",
        "add_youtube",
        "get_youtube_job",
        "resume_youtube_job",
    }
    assert received_settings == [settings]
    asyncio.run(server.call_tool("list_playlists", {}))
    asyncio.run(server.call_tool("get_playlist", {"card_id": "card-1"}))
    asyncio.run(
        server.call_tool(
            "add_mp3",
            {"card_id": "card-1", "chapter_key": "chapter-1", "file_path": "song.mp3"},
        )
    )
    asyncio.run(server.call_tool("rename_track", {"card_id": "card-1", "track_key": "track-1",
                                            "artist": "Example Artist", "title": "Example Song"}))
    asyncio.run(server.call_tool("remove_track", {"card_id": "card-1", "track_key": "track-1"}))
    asyncio.run(server.call_tool("upload_icon", {"file_path": "cat.png"}))
    asyncio.run(server.call_tool("set_track_icon", {"card_id": "card-1", "track_key": "track-1",
                                               "media_id": "abc123"}))
    asyncio.run(server.call_tool("reorder_chapters", {"card_id": "card-1", "chapter_keys": ["chapter-2", "chapter-1"]}))
    asyncio.run(server.call_tool("export_track", {"card_id": "card-1", "track_key": "track-1"}))

    assert client.calls == [
        ("list_playlists",),
        ("get_playlist", "card-1"),
        ("add_mp3", "card-1", "chapter-1", "song.mp3", True, None),
        ("rename_track", "card-1", "track-1", "Example Artist", "Example Song", True),
        ("remove_track", "card-1", "track-1", True),
        ("upload_icon", "cat.png", True, None, True),
        ("set_track_icon", "card-1", "track-1", "abc123", True),
        ("reorder_chapters", "card-1", ("chapter-2", "chapter-1"), True),
        ("export_track", "card-1", "track-1", None, True),
    ]


def test_default_factory_uses_auth_manager_token_and_write_settings(monkeypatch, tmp_path: Path):
    from yoto_mcp import server as server_module

    settings = Settings(client_id="test-client", upload_root=tmp_path, allow_writes=True)
    observed = {}

    class AuthStub:
        def __init__(self, given_settings, *, allow_interactive=True):
            observed["auth_settings"] = given_settings
            observed["interactive_auth"] = allow_interactive

        def token(self):
            return "injected-token"

    class ClientStub(RecordingClient):
        def __init__(self, token_provider, **kwargs):
            super().__init__()
            observed["token"] = token_provider()
            observed["client_options"] = kwargs

    monkeypatch.setattr(server_module, "AuthManager", AuthStub)
    monkeypatch.setattr(server_module, "YotoClient", ClientStub)

    server = create_server(settings)
    assert observed["interactive_auth"] is True

    assert {tool.name for tool in asyncio.run(server.list_tools())} == {
        "list_playlists",
        "get_playlist",
        "add_mp3",
        "rename_track",
        "remove_track",
        "inspect_mp3_metadata",
        "lookup_recordings",
        "upload_icon",
        "set_track_icon",
        "reorder_chapters",
        "export_track",
        "add_youtube",
        "get_youtube_job",
        "resume_youtube_job",
    }
    assert observed["auth_settings"] is settings
    assert observed["token"] == "injected-token"
    assert observed["client_options"] == {
        "upload_root": tmp_path,
        "allow_writes": True,
    }


def test_stdio_tools_can_list_pending_status_during_blocking_upload(tmp_path: Path):
    started = threading.Event()
    release = threading.Event()

    class SlowClient(RecordingClient):
        def __init__(self):
            super().__init__()
            self.list_during_upload = False

        def add_mp3(self, card_id, chapter_key, file_path, *, dry_run=True, title=None):
            started.set()
            release.wait(timeout=1)
            return {"dry_run": dry_run}

        def list_playlists(self):
            self.list_during_upload = started.is_set() and not release.is_set()
            return [{"cardId": "card-1", "pending_tracks": [{"status": "transcoding"}]}]

    client = SlowClient()
    server = create_server(Settings(client_id="test-client", upload_root=tmp_path),
                           client_factory=lambda _: client)

    async def exercise():
        timer = threading.Timer(0.4, release.set)
        timer.daemon = True
        timer.start()
        upload = asyncio.create_task(server.call_tool("add_mp3", {
            "card_id": "card-1", "chapter_key": "new", "file_path": "song.mp3",
            "dry_run": False,
        }))
        try:
            await asyncio.sleep(0.02)
            assert started.is_set() and not release.is_set()
            await asyncio.wait_for(server.call_tool("list_playlists", {}), timeout=0.15)
            assert client.list_during_upload
        finally:
            release.set()
            await upload
            timer.cancel()

    asyncio.run(exercise())


def test_metadata_tools_inspect_bounded_local_file_and_offer_candidates_without_yoto_auth(
    monkeypatch, tmp_path: Path,
):
    from yoto_mcp import server as server_module

    payloads = [
        ("TPE1", "Lenka"), ("TIT2", "Everything at Once"), ("TALB", "Two"),
    ]
    body = b"".join(
        name.encode() + bytes((0, 0, 0, len(text.encode()) + 1)) + b"\x00\x00\x03" + text.encode()
        for name, text in payloads
    )
    song = tmp_path / "song.mp3"
    song.write_bytes(b"ID3\x04\x00\x00" + bytes((0, 0, 0, len(body))) + body + b"\xff\xfb" + b"x" * 300)
    queries = []
    monkeypatch.setattr(server_module, "lookup_recordings", lambda q: queries.append(q) or [
        {"id": "recording-1", "title": "Everything at Once", "artist": "Lenka", "score": 99}
    ])
    client = RecordingClient()
    server = create_server(Settings(upload_root=tmp_path), client_factory=lambda _: client)

    inspected = asyncio.run(server.call_tool("inspect_mp3_metadata", {"file_path": "song.mp3"}))
    assert isinstance(inspected, CallToolResult)
    assert inspected.structured_content == {
        "file": "song.mp3", "artist": "Lenka", "title": "Everything at Once", "album": "Two",
        "suggested_title": "Lenka — Everything at Once", "source": "embedded_id3",
    }
    found = asyncio.run(server.call_tool("lookup_recordings", {"query": "Lenka Everything at Once"}))
    assert isinstance(found, CallToolResult)
    assert found.structured_content["result"][0]["id"] == "recording-1"
    assert queries == ["Lenka Everything at Once"]
    assert client.calls == []


def test_main_runs_mcp_over_stdio(monkeypatch):
    from yoto_mcp import __main__ as main_module

    calls = []

    class FakeServer:
        def run(self, *, transport):
            calls.append(transport)

    monkeypatch.setattr(main_module, "create_server", lambda settings: FakeServer())

    main_module.main([])

    assert calls == ["stdio"]


def test_add_youtube_tool_accepts_explicit_credits_and_returns_job_status(tmp_path: Path):
    seen = []

    class FakeYouTube:
        def submit(self, card_id, video_id, *, dry_run=True, artist=None, song_name=None):
            seen.append((card_id, video_id, dry_run, artist, song_name))
            return {"job_id": "job-one", "status": "queued"}

        def get(self, job_id):
            seen.append(("get", job_id))
            return {"job_id": job_id, "status": "complete", "stage": "preview"}

    settings = Settings(upload_root=tmp_path / "uploads", job_root=tmp_path / "jobs")
    server = create_server(
        settings, client_factory=lambda _: RecordingClient(),
        youtube_factory=lambda _client, _settings: FakeYouTube(),
    )
    submitted = asyncio.run(server.call_tool("add_youtube", {
        "card_id": "card-one", "video_id": "abcdefghijk",
        "artist": "Chosen Artist", "song_name": "Chosen Song",
    }))
    result = asyncio.run(server.call_tool("get_youtube_job", {"job_id": "job-one"}))
    assert submitted.structured_content == {"job_id": "job-one", "status": "queued"}
    assert result.structured_content == {
        "job_id": "job-one", "status": "complete", "stage": "preview",
    }
    assert seen == [
        ("card-one", "abcdefghijk", True, "Chosen Artist", "Chosen Song"),
        ("get", "job-one"),
    ]


def test_resume_youtube_job_tool_routes_exact_job_id(tmp_path: Path):
    seen = []

    class FakeYouTube:
        def resume(self, job_id, *, approve_duplicate=False, icon_media_id=None):
            seen.append((job_id, approve_duplicate, icon_media_id))
            return {"job_id": job_id, "status": "audio_added_icon_pending"}

    server = create_server(
        Settings(upload_root=tmp_path / "uploads", job_root=tmp_path / "jobs"),
        client_factory=lambda _: RecordingClient(),
        youtube_factory=lambda _client, _settings: FakeYouTube(),
    )
    result = asyncio.run(server.call_tool("resume_youtube_job", {
        "job_id": "job-one", "approve_duplicate": True, "icon_media_id": "known-icon",
    }))
    assert result.structured_content == {
        "job_id": "job-one", "status": "audio_added_icon_pending",
    }
    assert seen == [("job-one", True, "known-icon")]

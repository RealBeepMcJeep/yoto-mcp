"""MCP SDK v2 tool server for local Yoto playlist operations."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .auth import AuthManager
from .config import Settings
from .lyrics import lookup_lyric_evidence
from .media import resolve_mp3
from .metadata import format_track_title, lookup_recordings, read_mp3_tags
from .yoto import YotoClient
from .youtube_jobs import JobStore
from .youtube_pipeline import YouTubeCoordinator

ClientFactory = Callable[[Settings], Any]
YouTubeFactory = Callable[[Any, Settings], Any]


class BearerAuthMiddleware:
    """Require one configured bearer token for every Streamable HTTP MCP route."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self.expected = b"Bearer " + token.encode("utf-8")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if path == "/mcp" or path.startswith("/mcp/"):
            authorization_headers = [
                value for name, value in scope.get("headers", []) if name.lower() == b"authorization"
            ]
            if len(authorization_headers) != 1 or not hmac.compare_digest(
                authorization_headers[0], self.expected,
            ):
                response = Response(
                    "Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_http_app(server: MCPServer, settings: Settings) -> Any:
    """Create a protected Streamable HTTP app without changing the stdio server."""
    if len(settings.http_token) < 32:
        raise ValueError("YOTO_HTTP_TOKEN must be configured with at least 32 characters")
    hosts = list(settings.allowed_hosts)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"{scheme}://{host}" for scheme in ("http", "https") for host in hosts],
    )
    health_security = TransportSecurityMiddleware(security)

    async def healthz(request: Request) -> Response:
        rejected = await health_security.validate_request(request)
        return rejected if rejected is not None else JSONResponse({"status": "ok"})

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=security,
        host="0.0.0.0",
    )
    app.add_route("/healthz", healthz, methods=["GET"])
    app.add_middleware(BearerAuthMiddleware, token=settings.http_token)
    return app


def run_http_server(server: MCPServer, *, host: str, port: int, settings: Settings) -> None:
    """Run the configured Streamable HTTP ASGI app."""
    import uvicorn

    app = create_http_app(server, settings)
    uvicorn.run(app, host=host, port=port, log_level="info")


def create_server(
    settings: Settings, client_factory: ClientFactory | None = None,
    youtube_factory: YouTubeFactory | None = None,
    *, interactive_auth: bool = True,
) -> MCPServer:
    """Construct the stdio MCP server; injected factories keep tests offline."""
    if client_factory is None:
        auth = AuthManager(settings, allow_interactive=interactive_auth)
        client = YotoClient(
            auth.token,
            upload_root=settings.upload_root,
            allow_writes=settings.allow_writes,
        )
    else:
        client = client_factory(settings)
    if youtube_factory is not None:
        youtube = youtube_factory(client, settings)
    elif settings.job_root is not None and settings.upload_root is not None:
        youtube = YouTubeCoordinator(
            client, JobStore(settings.job_root), settings.upload_root,
            allow_writes=settings.allow_writes,
        )
    else:
        youtube = None

    server = MCPServer(
        name="yoto-mcp",
        title="Yoto playlists",
        description="Read and safely manage Yoto Make Your Own playlists.",
    )

    @server.tool(name="list_playlists", description="List Yoto playlists and current local upload status.")
    def list_playlists() -> list[dict[str, Any]]:
        return client.list_playlists()

    @server.tool(name="get_playlist", description="Read a playlist and its chapters, tracks, and local status.")
    def get_playlist(card_id: str) -> dict[str, Any]:
        return client.get_playlist(card_id)

    @server.tool(
        name="add_youtube",
        description=(
            "Start a durable background YouTube-to-Yoto job. Defaults to a private download/metadata/avatar "
            "preview without Yoto writes; provide dry_run=false for an authorized upload. Optional "
            "start_time/end_time select the original source timeline using M:SS[.mmm] or HH:MM:SS[.mmm]."
        ),
    )
    def add_youtube(
        card_id: str, video_id: str, dry_run: bool = True,
        artist: str | None = None, song_name: str | None = None,
        start_time: str | None = None, end_time: str | None = None,
    ) -> dict[str, Any]:
        if youtube is None:
            raise ValueError("YOTO_UPLOAD_ROOT and YOTO_JOB_ROOT must be configured")
        return youtube.submit(
            card_id, video_id, dry_run=dry_run, artist=artist, song_name=song_name,
            start_time=start_time, end_time=end_time,
        )

    @server.tool(name="get_youtube_job", description="Read persisted YouTube job progress, warnings, status, and safe upload-failure diagnostics by job ID.")
    def get_youtube_job(job_id: str) -> dict[str, Any]:
        if youtube is None:
            raise ValueError("YOTO_UPLOAD_ROOT and YOTO_JOB_ROOT must be configured")
        return youtube.get(job_id)

    @server.tool(name="resume_youtube_job", description="Reconcile audio, resume a pending icon, explicitly approve a flagged duplicate, or supply a verified icon ID after an uncertain upload. Never blindly re-add audio/icons.")
    def resume_youtube_job(
        job_id: str, approve_duplicate: bool = False, icon_media_id: str | None = None,
    ) -> dict[str, Any]:
        if youtube is None:
            raise ValueError("YOTO_UPLOAD_ROOT and YOTO_JOB_ROOT must be configured")
        return youtube.resume(
            job_id, approve_duplicate=approve_duplicate, icon_media_id=icon_media_id,
        )

    @server.tool(name="add_mp3", description="Upload an MP3 and append it to a playlist chapter.")
    def add_mp3(
        card_id: str,
        chapter_key: str,
        file_path: str,
        dry_run: bool = True,
        title: str | None = None,
    ) -> dict[str, Any]:
        return client.add_mp3(
            card_id,
            chapter_key,
            file_path,
            dry_run=dry_run,
            title=title,
        )

    @server.tool(name="rename_track", description="Preview or rename one exact track as Artist — Title; also rename its chapter when it has only one track.")
    def rename_track(
        card_id: str,
        track_key: str,
        artist: str,
        title: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return client.rename_track(card_id, track_key, artist, title, dry_run=dry_run)

    @server.tool(name="inspect_mp3_metadata", description="Read embedded ID3 artist/title/album from one MP3 under YOTO_UPLOAD_ROOT; no account request.")
    def inspect_mp3_metadata(file_path: str) -> dict[str, Any]:
        if settings.upload_root is None:
            raise ValueError("YOTO_UPLOAD_ROOT must be configured to inspect MP3s")
        source = resolve_mp3(settings.upload_root, file_path)
        tags = read_mp3_tags(source)
        suggestion = (format_track_title(tags["artist"], tags["title"])
                      if tags["artist"] and tags["title"] else tags["title"] or source.stem)
        return {"file": source.name, **tags, "suggested_title": suggestion,
                "source": "embedded_id3" if any(tags.values()) else "untagged"}

    @server.tool(name="lookup_recordings", description='Search MusicBrainz candidates only; use query like recording:"Song" AND artist:"Artist" for a precise match. Never auto-select or edit Yoto.')
    def lookup_recordings_tool(query: str) -> list[dict[str, Any]]:
        return lookup_recordings(query)

    @server.tool(
        name="lookup_lyric_evidence",
        description="Look up a recording's lyric candidate from LRCLIB (artist+title required; album/duration improve match precision). Text lookup only: no CPU transcription, no cache, no Yoto access. Returns a status, never invents lyrics.",
    )
    def lookup_lyric_evidence_tool(
        artist: str, title: str, album: str | None = None, duration_seconds: int | None = None,
    ) -> dict[str, Any]:
        return lookup_lyric_evidence(artist, title, album=album, duration_seconds=duration_seconds)

    @server.tool(name="remove_track", description="Remove one exact track key from a playlist.")
    def remove_track(card_id: str, track_key: str, dry_run: bool = True) -> dict[str, Any]:
        return client.remove_track(card_id, track_key, dry_run=dry_run)

    @server.tool(
        name="remove_empty_chapter",
        description="Remove one exactly matched chapter only when its tracks list is empty; defaults to a no-write preview.",
    )
    def remove_empty_chapter(
        card_id: str,
        chapter_key: str,
        expected_title: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return client.remove_empty_chapter(
            card_id, chapter_key, expected_title, dry_run=dry_run,
        )

    @server.tool(
        name="upload_icon",
        description="Upload a local PNG/JPEG/GIF under YOTO_UPLOAD_ROOT as a Yoto custom icon; returns a mediaId for set_track_icon. Does not assign it to anything.",
    )
    def upload_icon(
        file_path: str,
        auto_convert: bool = True,
        filename: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return client.upload_icon(file_path, auto_convert=auto_convert, filename=filename, dry_run=dry_run)

    @server.tool(
        name="set_track_icon",
        description="Assign an already-uploaded icon (by mediaId) to one exact track; also sets its chapter's icon when that chapter has only one track.",
    )
    def set_track_icon(card_id: str, track_key: str, media_id: str, dry_run: bool = True) -> dict[str, Any]:
        return client.set_track_icon(card_id, track_key, media_id, dry_run=dry_run)

    @server.tool(
        name="reorder_chapters",
        description="Reorder a playlist's chapters (its songs) to an exact given order; chapter_keys must list every existing chapter key exactly once. For a shuffle, fetch the current order and pass a randomized permutation.",
    )
    def reorder_chapters(card_id: str, chapter_keys: list[str], dry_run: bool = True) -> dict[str, Any]:
        return client.reorder_chapters(card_id, chapter_keys, dry_run=dry_run)

    @server.tool(
        name="export_track",
        description="Download one owned track's current playable audio to a local file under YOTO_UPLOAD_ROOT. Not guaranteed byte-identical to any original upload; the signed URL itself is never returned.",
    )
    def export_track(
        card_id: str,
        track_key: str,
        destination_name: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return client.export_track(card_id, track_key, destination_name=destination_name, dry_run=dry_run)

    return server

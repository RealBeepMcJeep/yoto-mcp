from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from yoto_mcp import uploads
from yoto_mcp.config import Settings
from yoto_mcp.server import create_http_app, create_server
from yoto_mcp.uploads import INBOX_RETENTION_SECONDS, LINK_TTL_SECONDS, UploadRejected

# 0.5 s, 8 kHz mono sine; ffprobe reads it as codec mp3.
TINY_MP3 = base64.b64decode(
    "/+MYxAAK8AbVuUEAAv9HcNtgBg+D4PvUCAIHIPg+fxOD4PoggcRB8H34IOu/wxwG/SGOXfznT7ulJoc4WcQ3/GVEJRyhC3+D/+MYxA8QmUKQAZRQ"
    "AG8H6ge5ABlxEnAPiYDmR1gd6mHvgGgCQCorCKC6//IR6KpEPh8ab//5CgNCUJA1/4lOg0r//////////+MYxAcOaKY8Ad4AAP/////40iEYA4AS"
    "SQXAKMCsF4wfwajCHFKMQulwxvhNjDbBaMD0D4FAXiEAMDAKoSW07dP//vgSpVAg/+MYxAgNqKYsAFa8YCmJFGfQGycnl9GIANucd+h5p+CpmGqC"
    "8YJQC5QA8gRBoACAVL6x////3+U///9a/1//fGMDIYEAAdcF/+MYxAwO6KYsAKewgIIBpyIHmpGIyKqc5FMpq8iHmHkCsYMoFpgYARG6RjOX8Urx"
    "///Z/v9P////////Rf//+TsVQOAwSYQN/+MYxAsNaKYwAAb+RJgKECJ0AhGBgSyEkYCKDdEgESDADgwAMAVCAAsiAEECa4b/////+j///1L///4x"
    "gBgAA30DACAcDAsB/+MYxBANOKZAAVYAACAQC8DEaMsDH2bUDq7FsDekP4DBsCADAMAkAIF4CQCiPhmBCiEgEj/HMIIwyn+MwVBZggpIuB2k4jeP"
    "/+MYxBYSiS6UKZRoAIA5NITgLgBtwDgYSK//gnYK+PMd4mZT//8zL5usvhc//y4nPh8u//6AfAZomNf//QTcauQqO1ct2AA//+MYxAYM8LbRucMA"
    "A/2Zl/4zMpcYMzNxmZtVAm/VVAQp/QUGCvBQUKf4KDHf0FChvgoMFP8FChv+SCpMQU1FMy4xMDCqqqqq"
)


# 1x1 PNG.
TINY_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


def _fake_probe(path: Path) -> float:
    if path.read_bytes() != TINY_MP3:
        raise UploadRejected("File is not a readable MP3")
    return 0.5


@pytest.fixture
def upload_app(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(uploads, "probe_mp3", _fake_probe)
    root = tmp_path / "uploads"
    root.mkdir()
    settings = Settings(http_token="t" * 40, upload_root=root)
    app = create_http_app(create_server(settings, client_factory=lambda _: object()), settings)
    return app, root


def test_create_mp3_upload_link_accepts_one_mp3_put_that_add_mp3_can_use(tmp_path: Path, monkeypatch):
    import asyncio
    import socket

    import httpx2
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    monkeypatch.setattr(uploads, "probe_mp3", _fake_probe)
    root = tmp_path / "uploads"
    root.mkdir()
    token = "t" * 40
    settings = Settings(http_token=token, upload_root=root)
    mcp_server = create_server(settings, client_factory=lambda _: object())
    app = create_http_app(mcp_server, settings)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    web_server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))

    async def scenario():
        task = asyncio.create_task(web_server.serve(sockets=[listener]))
        try:
            for _ in range(200):
                if web_server.started:
                    break
                await asyncio.sleep(0.01)
            async with (
                httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as authed,
                streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=authed) as (r, w),
                ClientSession(r, w) as session,
            ):
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                created = await session.call_tool("create_upload", {"filename": "Some Artist - Some Song.mp3"})
                legacy = await session.call_tool("create_mp3_upload", {})
            assert "curl" in tools["create_upload"].description
            assert "create_upload" in tools["add_mp3"].description
            assert "create_upload" in tools["upload_icon"].description
            assert legacy.structured_content["upload_url"].startswith(f"http://127.0.0.1:{port}/uploads/")
            link = created.structured_content
            assert link["upload_url"].startswith(f"http://127.0.0.1:{port}/uploads/")
            assert link["expires_in_seconds"] == LINK_TTL_SECONDS
            assert token not in str(link)
            async with httpx2.AsyncClient() as anonymous:  # The link alone is the credential.
                first = await anonymous.put(link["upload_url"], content=TINY_MP3)
                replay = await anonymous.put(link["upload_url"], content=TINY_MP3)
            return first, replay
        finally:
            web_server.should_exit = True
            await asyncio.wait_for(task, timeout=3)

    try:
        first, replay = asyncio.run(scenario())
    finally:
        listener.close()

    assert first.status_code == 201
    body = first.json()
    assert body["kind"] == "mp3"
    assert body["file_path"].startswith("inbox/") and body["file_path"].endswith("/Some Artist - Some Song.mp3")
    assert body["size_bytes"] == len(TINY_MP3) and body["duration_seconds"] == 0.5
    assert body["suggested_title"] == "Some Artist - Some Song"
    assert "add_mp3" in body["next_step"]
    stored = root / body["file_path"]
    assert stored.read_bytes() == TINY_MP3
    assert stored.stat().st_mode & 0o777 == 0o600
    from yoto_mcp.media import resolve_mp3
    assert resolve_mp3(root, body["file_path"]) == stored.resolve()  # add_mp3's own path rule accepts it.
    assert replay.status_code == 404


def test_upload_link_rules(upload_app):
    app, root = upload_app
    links = app.state.upload_links
    with TestClient(app, base_url="http://localhost") as http:
        put = lambda token, **kw: http.put(f"/uploads/{token}", **kw)

        assert put("never-issued", content=TINY_MP3).status_code == 404

        forged = links.create()
        assert put(forged, content=TINY_MP3, headers={"host": "forged.invalid"}).status_code == 421
        assert put(forged, content=TINY_MP3).status_code == 201  # A rejected Host did not spend the link.

        bad = links.create()
        response = put(bad, content=b"not an mp3 " * 20)
        assert response.status_code == 400 and "MP3" in response.json()["error"]
        assert put(bad, content=TINY_MP3).status_code == 404  # A failed attempt still spends the link.

        tiny = links.create()
        assert put(tiny, content=b"x").status_code == 400

        form = links.create()
        assert http.put(f"/uploads/{form}", files={"file": ("a.mp3", TINY_MP3)}).status_code == 415

        huge = links.create()
        assert put(huge, content=TINY_MP3, headers={"content-length": str(200 * 1024 * 1024)}).status_code == 413

        expired = links.create()
        links.clock = lambda: time.time() + LINK_TTL_SECONDS + 1
        assert put(expired, content=TINY_MP3).status_code == 404

    stored = [path for path in (root / "inbox").rglob("*") if path.is_file()]
    assert len(stored) == 1  # Only the accepted upload remains; rejected ones are removed.


def test_upload_link_accepts_icon_images_for_upload_icon(upload_app, monkeypatch):
    from yoto_mcp.media import resolve_image

    app, root = upload_app
    links = app.state.upload_links
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 200
    with TestClient(app, base_url="http://localhost") as http:
        png = http.put(f"/uploads/{links.create('channel avatar.png')}", content=TINY_PNG)
        # The type comes from the bytes, not the requested name.
        misnamed = http.put(f"/uploads/{links.create('avatar.png')}", content=jpeg)
        default_name = http.put(f"/uploads/{links.create()}", content=TINY_PNG)
        monkeypatch.setattr(uploads, "MAX_IMAGE_BYTES", 100)
        too_big = http.put(f"/uploads/{links.create()}", content=TINY_PNG + b"\x00" * 100)

    assert png.status_code == 201
    body = png.json()
    assert body["kind"] == "image" and body["content_type"] == "image/png"
    assert body["file_path"].endswith("/channel avatar.png") and body["size_bytes"] == len(TINY_PNG)
    assert "upload_icon" in body["next_step"] and "set_track_icon" in body["next_step"]
    assert resolve_image(root, body["file_path"])[1] == "image/png"  # upload_icon's own check accepts it.
    assert (root / body["file_path"]).stat().st_mode & 0o777 == 0o600
    assert misnamed.status_code == 201 and misnamed.json()["file_path"].endswith("/avatar.jpg")
    assert misnamed.json()["content_type"] == "image/jpeg"
    assert default_name.json()["file_path"].endswith("/upload.png")
    assert too_big.status_code == 413 and "10 MiB" in too_big.json()["error"]


def test_new_link_sweeps_inbox_uploads_older_than_a_day(upload_app):
    app, root = upload_app
    links = app.state.upload_links
    old = root / "inbox" / "stale"
    old.mkdir(parents=True)
    (old / "old.mp3").write_bytes(TINY_MP3)
    fresh = root / "inbox" / "fresh"
    fresh.mkdir()
    long_ago = time.time() - INBOX_RETENTION_SECONDS - 60
    os.utime(old, (long_ago, long_ago))
    links.create()
    assert not old.exists() and fresh.exists()


def test_upload_tool_is_http_only(tmp_path: Path):
    import asyncio

    settings = Settings(upload_root=tmp_path)
    tools = asyncio.run(create_server(settings, client_factory=lambda _: object()).list_tools())
    assert not {"create_upload", "create_mp3_upload"} & {tool.name for tool in tools}


def test_real_ffprobe_accepts_the_fixture_and_rejects_junk(tmp_path: Path):
    try:
        uploads._media_binary("ffprobe")
    except Exception:  # noqa: BLE001
        pytest.skip("ffprobe is not installed at the configured absolute path")
    good, junk = tmp_path / "good.mp3", tmp_path / "junk.mp3"
    good.write_bytes(TINY_MP3)
    junk.write_bytes(b"\x00" * 4096)
    assert 0.3 < uploads.probe_mp3(good) < 1.0
    with pytest.raises(UploadRejected):
        uploads.probe_mp3(junk)

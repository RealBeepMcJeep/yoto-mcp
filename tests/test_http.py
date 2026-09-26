from __future__ import annotations

from yoto_mcp.config import Settings
from yoto_mcp.server import BearerAuthMiddleware, create_http_app, create_server


def test_http_settings_read_token_and_allowed_hosts_without_exposing_token():
    secret = "a" * 40
    settings = Settings.from_env({
        "YOTO_HTTP_TOKEN": secret,
        "YOTO_ALLOWED_HOSTS": "yoto-mcp,localhost:9000",
    })

    assert settings.http_token == secret
    assert settings.allowed_hosts == ("yoto-mcp", "localhost:9000")
    assert secret not in repr(settings)


def test_http_health_is_minimal_and_every_mcp_method_requires_bearer_auth():
    from starlette.testclient import TestClient

    class FakeClient:
        def __init__(self):
            self.calls = []

        def list_playlists(self):
            self.calls.append("list_playlists")
            return [{"cardId": "card-1"}]

        def get_playlist(self, card_id):
            self.calls.append(("get_playlist", card_id))
            return {"cardId": card_id}

        def add_mp3(self, *args, **kwargs):
            self.calls.append(("add_mp3", args, kwargs))
            return {"dry_run": kwargs.get("dry_run", True)}

    secret = "s" * 40
    settings = Settings(http_token=secret)
    client = FakeClient()
    mcp_server = create_server(settings, client_factory=lambda _: client)
    app = create_http_app(mcp_server, settings)

    with TestClient(app) as http:
        health = http.get("/healthz", headers={"host": "localhost"})
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert http.get("/healthz", headers={"host": "untrusted.invalid"}).status_code == 421

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_mp3",
                "arguments": {"card_id": "c", "chapter_key": "ch", "file_path": "song.mp3"},
            },
        }
        for method in ("GET", "POST", "DELETE"):
            response = http.request(method, "/mcp", json=request if method == "POST" else None)
            assert response.status_code == 401
            assert secret not in response.text

        wrong_token = http.post(
            "/mcp",
            headers={"authorization": "Bearer invalid"},
            json=request,
        )
        assert wrong_token.status_code == 401
        assert secret not in wrong_token.text

    assert client.calls == []


def test_http_rejects_forged_host_on_authenticated_mcp_request():
    from starlette.testclient import TestClient

    settings = Settings(http_token="s" * 40)
    mcp_server = create_server(settings, client_factory=lambda _: object())
    app = create_http_app(mcp_server, settings)

    with TestClient(app) as http:
        response = http.post(
            "/mcp",
            headers={"authorization": f"Bearer {settings.http_token}", "host": "forged.invalid"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert response.status_code == 421
    assert "forged.invalid" not in response.text


def test_serve_cli_selects_streamable_http_and_preserves_stdio_default(monkeypatch):
    from yoto_mcp import __main__ as main_module

    calls = []

    class FakeServer:
        pass

    server = FakeServer()
    settings = Settings(http_token="s" * 40)
    monkeypatch.setattr(
        main_module, "create_server",
        lambda given, *, interactive_auth=True: calls.append(("create", given, interactive_auth)) or server,
    )
    monkeypatch.setattr(
        main_module,
        "run_http_server",
        lambda given_server, *, host, port, settings: calls.append(
            ("http", given_server, host, port, settings)
        ),
    )
    monkeypatch.setattr(main_module.Settings, "from_env", lambda: settings)

    main_module.main([
        "serve", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000",
    ])

    assert calls == [
        ("create", settings, False),
        ("http", server, "0.0.0.0", 8000, settings),
    ]


def test_bearer_middleware_rejects_ambiguous_duplicate_authorization_headers():
    import asyncio

    token = "x" * 40
    downstream_called = False
    sent = []

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [
            (b"authorization", f"Bearer {token}".encode()),
            (b"authorization", b"Bearer attacker"),
        ],
    }
    middleware = BearerAuthMiddleware(downstream, token)
    asyncio.run(middleware(scope, receive, send))

    assert not downstream_called
    assert sent[0]["status"] == 401


def test_loopback_streamable_http_initialize_list_and_read_with_fake_yoto():
    import asyncio
    import socket

    import httpx2
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    class FakeYotoClient:
        def list_playlists(self):
            return [{"cardId": "card-1", "title": "Offline fixture"}]

        def get_playlist(self, card_id):
            return {"cardId": card_id, "title": "Offline fixture"}

    token = "t" * 40
    settings = Settings(http_token=token)
    mcp_server = create_server(settings, client_factory=lambda _: FakeYotoClient())
    app = create_http_app(mcp_server, settings)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    web_server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))

    async def smoke():
        task = asyncio.create_task(web_server.serve(sockets=[listener]))
        try:
            for _ in range(200):
                if web_server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            assert web_server.started
            async with httpx2.AsyncClient() as unauthenticated:
                health = await unauthenticated.get(f"http://127.0.0.1:{port}/healthz")
                assert health.status_code == 200
                assert health.json() == {"status": "ok"}
                rejected = await unauthenticated.post(
                    f"http://127.0.0.1:{port}/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
                assert rejected.status_code == 401
            async with (
                httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as client,
                streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp", http_client=client,
                ) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                initialized = await session.initialize()
                tools = await session.list_tools()
                playlists = await session.call_tool("list_playlists")
                playlist = await session.call_tool("get_playlist", {"card_id": "card-1"})
            assert initialized.server_info.name == "yoto-mcp"
            assert "list_playlists" in {tool.name for tool in tools.tools}
            assert playlists.structured_content["result"][0]["cardId"] == "card-1"
            assert playlist.structured_content["cardId"] == "card-1"
        finally:
            web_server.should_exit = True
            await asyncio.wait_for(task, timeout=3)

    try:
        asyncio.run(smoke())
    finally:
        listener.close()


def test_http_app_fails_closed_for_missing_or_short_bearer_token():
    import pytest

    for token in ("", "short", "s" * 31):
        settings = Settings(http_token=token)
        mcp_server = create_server(settings, client_factory=lambda _: object())
        with pytest.raises(ValueError, match="YOTO_HTTP_TOKEN"):
            create_http_app(mcp_server, settings)

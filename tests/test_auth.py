import base64
import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from yoto_mcp.auth import AuthError, AuthManager, SessionStore
from yoto_mcp.config import Settings


def _jwt(exp: float) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def test_session_store_persists_only_token_fields_with_private_mode(tmp_path: Path):
    destination = tmp_path / "state" / "session.json"
    store = SessionStore(destination)
    store.save({
        "access_token": "example-access",
        "refresh_token": "example-refresh",
        "expires_at": 4000000000,
        "extra": "must not be persisted",
    })
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
        assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    loaded = store.load()
    assert loaded["access_token"] == "example-access"
    assert loaded["refresh_token"] == "example-refresh"
    assert "extra" not in loaded


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX permission bits to reject")
def test_session_store_rejects_world_readable_file(tmp_path: Path):
    destination = tmp_path / "session.json"
    destination.write_text(json.dumps({"access_token": "secret"}))
    destination.chmod(0o644)
    try:
        SessionStore(destination).load()
    except ValueError as exc:
        assert "permissions" in str(exc)
    else:
        raise AssertionError("session file must fail closed")


def _fake_opener_hitting_callback(code: str, captured: dict):
    def opener(auth_url: str) -> None:
        captured["auth_url"] = auth_url

        def hit() -> None:
            httpx.Client().get("http://127.0.0.1:8787/callback", params={"code": code})

        threading.Thread(target=hit, daemon=True).start()

    return opener


def test_first_login_uses_pkce_and_persists_rotating_refresh_token(tmp_path: Path):
    captured: dict = {}
    requests = []

    def reply(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        body = parse_qs(req.content.decode())
        assert body["grant_type"] == ["authorization_code"]
        assert body["client_id"] == ["public-client"]
        assert body["redirect_uri"] == ["http://127.0.0.1:8787/callback"]
        verifier = body["code_verifier"][0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        auth_params = parse_qs(urlsplit(captured["auth_url"]).query)
        assert auth_params["code_challenge"] == [challenge]
        assert auth_params["code_challenge_method"] == ["S256"]
        assert auth_params["scope"] == ["user:content:manage user:content:view user:icons:manage offline_access"]
        return httpx.Response(200, json={"access_token": _jwt(time.time() + 3600), "refresh_token": "refresh-1"})

    settings = Settings.from_env({"YOTO_CLIENT_ID": "public-client", "YOTO_SESSION_FILE": str(tmp_path / "session.json")})
    manager = AuthManager(
        settings,
        transport=httpx.MockTransport(reply),
        opener=_fake_opener_hitting_callback("auth-code", captured),
    )
    token = manager.token()
    assert token
    assert len(requests) == 1
    saved = SessionStore(tmp_path / "session.json").load()
    assert saved["refresh_token"] == "refresh-1"
    assert "code_verifier" not in json.dumps(saved)


def test_saved_access_token_is_reused_until_it_nears_expiry(tmp_path: Path):
    store = SessionStore(tmp_path / "session.json")
    store.save({"access_token": "still-good", "refresh_token": "r", "expires_at": 5000})

    def reply(req: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call the network for a still-valid token")

    settings = Settings.from_env({"YOTO_CLIENT_ID": "public-client", "YOTO_SESSION_FILE": str(store.path)})
    manager = AuthManager(settings, transport=httpx.MockTransport(reply), now=lambda: 1000)
    assert manager.token() == "still-good"


def test_expired_access_token_triggers_single_use_refresh(tmp_path: Path):
    store = SessionStore(tmp_path / "session.json")
    store.save({"access_token": "stale", "refresh_token": "refresh-old", "expires_at": 1000})
    requests = []

    def reply(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        body = parse_qs(req.content.decode())
        assert body["grant_type"] == ["refresh_token"]
        assert body["refresh_token"] == ["refresh-old"]
        assert body["client_id"] == ["public-client"]
        return httpx.Response(200, json={"access_token": _jwt(9999), "refresh_token": "refresh-new"})

    settings = Settings.from_env({"YOTO_CLIENT_ID": "public-client", "YOTO_SESSION_FILE": str(store.path)})
    manager = AuthManager(settings, transport=httpx.MockTransport(reply), now=lambda: 5000)
    assert manager.token()
    assert len(requests) == 1
    saved = store.load()
    assert saved["refresh_token"] == "refresh-new"


def test_missing_client_id_fails_before_any_network_call(tmp_path: Path):
    settings = Settings.from_env({"YOTO_SESSION_FILE": str(tmp_path / "session.json")})

    def reply(req: httpx.Request) -> httpx.Response:
        raise AssertionError("must not contact Yoto without a client id")

    manager = AuthManager(settings, transport=httpx.MockTransport(reply))
    with pytest.raises(AuthError, match="YOTO_CLIENT_ID"):
        manager.token()


def test_http_auth_without_session_never_opens_interactive_callback(tmp_path: Path):
    settings = Settings.from_env({"YOTO_CLIENT_ID": "public-client", "YOTO_SESSION_FILE": str(tmp_path / "session.json")})
    manager = AuthManager(settings, allow_interactive=False, opener=lambda _: pytest.fail("browser opened"))
    with pytest.raises(AuthError, match="session"):
        manager.token()


def test_concurrent_token_calls_refresh_single_use_token_only_once(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor

    store = SessionStore(tmp_path / "session.json")
    store.save({"access_token": "stale", "refresh_token": "only-once", "expires_at": 1000})
    seen = []
    barrier = threading.Barrier(2)

    def reply(req: httpx.Request) -> httpx.Response:
        seen.append(parse_qs(req.content.decode())["refresh_token"][0])
        time.sleep(0.03)
        return httpx.Response(200, json={"access_token": _jwt(9999), "refresh_token": "new"})

    settings = Settings.from_env({"YOTO_CLIENT_ID": "public-client", "YOTO_SESSION_FILE": str(store.path)})
    manager = AuthManager(settings, transport=httpx.MockTransport(reply), now=lambda: 5000)

    def token():
        barrier.wait(timeout=2)
        return manager.token()

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(lambda _: token(), range(2)))
    assert values[0] == values[1]
    assert seen == ["only-once"]


def test_token_endpoint_failure_raises_auth_error(tmp_path: Path):
    store = SessionStore(tmp_path / "session.json")
    store.save({"access_token": "stale", "refresh_token": "bad", "expires_at": 1000})

    def reply(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    settings = Settings.from_env({"YOTO_CLIENT_ID": "public-client", "YOTO_SESSION_FILE": str(store.path)})
    manager = AuthManager(settings, transport=httpx.MockTransport(reply), now=lambda: 5000)
    with pytest.raises(AuthError):
        manager.token()


def test_non_jwt_access_token_is_rejected(tmp_path: Path):
    def reply(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "not-a-jwt", "refresh_token": "r"})

    settings = Settings.from_env({"YOTO_CLIENT_ID": "public-client", "YOTO_SESSION_FILE": str(tmp_path / "session.json")})
    manager = AuthManager(
        settings,
        transport=httpx.MockTransport(reply),
        opener=_fake_opener_hitting_callback("auth-code", {}),
    )
    with pytest.raises(AuthError, match="JWT"):
        manager.token()


def test_callback_timeout_raises_auth_error(tmp_path: Path):
    from yoto_mcp import auth as auth_module

    settings = Settings.from_env({"YOTO_CLIENT_ID": "public-client", "YOTO_SESSION_FILE": str(tmp_path / "session.json")})
    manager = AuthManager(settings, opener=lambda url: None)
    original_timeout = auth_module.CALLBACK_TIMEOUT
    auth_module.CALLBACK_TIMEOUT = 1
    try:
        with pytest.raises(AuthError, match="timed out"):
            manager.token()
    finally:
        auth_module.CALLBACK_TIMEOUT = original_timeout

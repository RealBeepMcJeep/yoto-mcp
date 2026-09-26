"""Official Yoto OAuth (Authorization Code + PKCE, public client).

Follows https://yoto.dev/authentication/headless-cli-auth: a one-time
browser sign-in caught by a loopback callback server, then silent
refresh-token renewal on every later run. No client secret is used or
stored; public clients don't have one.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import stat
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .config import Settings

IDP = "https://login.yotoplay.com"
AUTHORIZE_URL = f"{IDP}/authorize"
TOKEN_URL = f"{IDP}/oauth/token"
AUDIENCE = "https://api.yotoplay.com"
# Least privilege for this tool: manage MYO content/icons, plus offline_access for
# refresh. Requested explicitly rather than relying on user:content:manage's documented
# "Includes" scopes: GET /content/{cardId} was confirmed (403 body) to check the literal
# user:content:view string, not a manage->view hierarchy, despite what the docs describe.
SCOPES = "user:content:manage user:content:view user:icons:manage offline_access"
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8787
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"
CALLBACK_TIMEOUT = 300


class AuthError(Exception):
    """A safe, static error message; never display raw IdP responses or tokens."""


class SessionStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
        except FileNotFoundError:
            return {}
        # Windows has no POSIX permission bits (chmod only toggles read-only);
        # per-user access is already enforced by the profile directory's ACLs.
        if os.name != "nt" and mode & 0o077:
            raise ValueError("Session file permissions must be private (0600)")
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict):
            raise TypeError("Invalid session file")
        return data

    def save(self, data: dict[str, Any]) -> None:
        safe = {key: data[key] for key in ("access_token", "refresh_token", "expires_at") if key in data}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        fd, name = tempfile.mkstemp(prefix=".session-", dir=self.path.parent)
        try:
            # fchmod is POSIX-only; Windows has no per-fd permission bits.
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(safe, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
            self.path.chmod(0o600)
        finally:
            # Best-effort: os.replace already renamed the temp file away on
            # success. This only fires on failure, and on Windows a transient
            # AV/indexer lock can briefly hold the leftover file too.
            try:
                if os.path.exists(name):
                    os.unlink(name)
            except OSError:
                pass


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        code = query.get("code", [None])[0]
        if code:
            self.server.received_code = code  # type: ignore[attr-defined]
            body = b"Login complete! You can close this tab and return to your terminal."
            self.send_response(200)
        else:
            body = b"Login failed: no authorization code was received."
            self.send_response(400)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence default stderr access log
        pass


class AuthManager:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], float] = time.time,
        opener: Callable[[str], object] = webbrowser.open,
        allow_interactive: bool = True,
    ):
        self.settings = settings
        self.store = SessionStore(settings.session_file)
        self.now = now
        self.opener = opener
        self.allow_interactive = allow_interactive
        self._token_lock = threading.Lock()
        self._http = httpx.Client(transport=transport, timeout=30.0)

    def token(self) -> str:
        # The IdP rotates single-use refresh tokens; serialize read/refresh/save
        # among concurrent tool calls within this server process.
        with self._token_lock:
            return self._token_locked()

    def _token_locked(self) -> str:
        saved = self.store.load()
        if saved.get("access_token") and saved.get("expires_at", 0) > self.now() + 30:
            return str(saved["access_token"])
        if saved.get("refresh_token"):
            return self._request_token({
                "grant_type": "refresh_token",
                "client_id": self.settings.client_id,
                "refresh_token": saved["refresh_token"],
            })
        if not self.allow_interactive:
            raise AuthError("Yoto session unavailable; complete consent locally and mount a private session")
        return self._interactive_login()

    def _interactive_login(self) -> str:
        if not self.settings.client_id:
            raise AuthError("YOTO_CLIENT_ID is required for first-time login")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {
            "audience": AUDIENCE,
            "scope": SCOPES,
            "response_type": "code",
            "client_id": self.settings.client_id,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "redirect_uri": REDIRECT_URI,
        }
        auth_url = f"{AUTHORIZE_URL}?{urlencode(params)}"
        code = self._await_callback(auth_url)
        return self._request_token({
            "grant_type": "authorization_code",
            "client_id": self.settings.client_id,
            "code_verifier": verifier,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        })

    def _await_callback(self, auth_url: str) -> str:
        try:
            server = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _CallbackHandler)
        except OSError as exc:
            raise AuthError(f"Could not bind the local callback server on port {REDIRECT_PORT}") from exc
        server.received_code = None  # type: ignore[attr-defined]
        server.timeout = CALLBACK_TIMEOUT
        try:
            print("To sign in to Yoto, open this URL in your browser:")
            print(auth_url)
            try:
                self.opener(auth_url)
            except Exception:  # noqa: S110, BLE001 - opening a browser is a convenience, not required
                pass
            server.handle_request()
        finally:
            server.server_close()
        code = server.received_code  # type: ignore[attr-defined]
        if not code:
            raise AuthError("Yoto login timed out or did not return an authorization code")
        return code

    def _request_token(self, payload: dict[str, str]) -> str:
        try:
            response = self._http.post(
                TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code != 200:
                raise AuthError("Yoto token request failed; you may need to sign in again")
            issued = response.json()
            access = issued.get("access_token")
            refresh = issued.get("refresh_token")
            if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
                raise AuthError("Yoto token response was invalid")
            expires_at = self._decode_expiry(access)
        except AuthError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise AuthError("Yoto token exchange failed") from None
        # Refresh tokens are single-use: always persist the newly issued one.
        self.store.save({"access_token": access, "refresh_token": refresh, "expires_at": expires_at})
        return access

    @staticmethod
    def _decode_expiry(token: str) -> float:
        try:
            payload_segment = token.split(".")[1]
            padded = payload_segment + "=" * (-len(payload_segment) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded))
            return float(claims["exp"])
        except (IndexError, ValueError, KeyError, TypeError):
            raise AuthError("Yoto access token was not a valid JWT") from None

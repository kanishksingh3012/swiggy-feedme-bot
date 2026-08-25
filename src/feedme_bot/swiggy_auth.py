"""Swiggy MCP OAuth 2.1 PKCE.

Verified live (see plan notes): S256 PKCE, /auth/authorize -> browser
phone+OTP consent -> /auth/token exchange. Access tokens last 5 days
(expires_in=432000) and there is NO refresh_token — don't build a
refresh-grant path, the only recovery from a 401 is re-running this full
flow.

/auth/authorize requires a client_id, which the docs don't spell out —
confirmed live that Dynamic Client Registration (POST /auth/register,
RFC 7591) returns one (client_id "swiggy-mcp" for a public/PKCE client,
no secret). Registering fresh on every login rather than caching it —
the call is fast and idempotent, not worth the extra persisted state.
"""

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx

from feedme_bot.config import settings

CALLBACK_PORT = 8765
CALLBACK_PATH = "/callback"


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    received_code: str | None = None

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == CALLBACK_PATH:
            params = urllib.parse.parse_qs(parsed.query)
            _CallbackHandler.received_code = params.get("code", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"FeedMe Bot: Swiggy login complete, you can close this tab.")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: Any) -> None:  # silence default stderr logging
        pass


def _wait_for_auth_code(timeout_seconds: int = 300) -> str:
    _CallbackHandler.received_code = None
    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    deadline = time.time() + timeout_seconds
    try:
        while _CallbackHandler.received_code is None:
            if time.time() > deadline:
                raise TimeoutError("Timed out waiting for Swiggy login callback")
            time.sleep(0.25)
        return _CallbackHandler.received_code
    finally:
        server.shutdown()


def _register_client(redirect_uri: str) -> str:
    response = httpx.post(
        f"{settings.swiggy_mcp_base_url}/auth/register",
        json={
            "redirect_uris": [redirect_uri],
            "client_name": "FeedMe Bot",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
    )
    response.raise_for_status()
    return response.json()["client_id"]


def login() -> dict[str, Any]:
    """Run the full interactive PKCE flow and persist the resulting token."""
    verifier, challenge = _pkce_pair()
    redirect_uri = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"
    client_id = _register_client(redirect_uri)

    authorize_url = f"{settings.swiggy_mcp_base_url}/auth/authorize?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    webbrowser.open(authorize_url)
    code = _wait_for_auth_code()

    response = httpx.post(
        f"{settings.swiggy_mcp_base_url}/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "client_id": client_id,
        },
    )
    response.raise_for_status()
    token_data = response.json()
    token_data["obtained_at"] = time.time()

    settings.swiggy_credentials_path.parent.mkdir(parents=True, exist_ok=True)
    settings.swiggy_credentials_path.write_text(json.dumps(token_data, indent=2))
    return token_data


def load_token() -> str:
    """Return a usable access token, prompting an interactive login if needed."""
    path: Path = settings.swiggy_credentials_path
    if not path.exists():
        return login()["access_token"]

    token_data = json.loads(path.read_text())
    obtained_at = token_data.get("obtained_at", 0)
    expires_in = token_data.get("expires_in", 0)
    if time.time() > obtained_at + expires_in:
        # No refresh grant exists (v1.0) — the only recovery is a full re-login.
        return login()["access_token"]

    return token_data["access_token"]

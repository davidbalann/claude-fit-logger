"""
Minimal OAuth 2.1 shim, single-user only.

Claude's custom connector currently requires an MCP server to speak OAuth
2.1 with Dynamic Client Registration, even for a personal, single-user
server with no real login system. This module exists purely to satisfy
that protocol requirement — there is no real "authorize" screen, because
there's only one legitimate user (you) and the server is already gated by
a private URL + secret. It auto-approves every authorize request and hands
back your existing MCP_API_KEY as the access token underneath.

Known limitation: registered clients and pending auth codes are stored in
memory, not Neon. A Render redeploy or cold-start restart clears them,
which just means reconnecting the Claude connector once afterward — not a
security issue, just a minor inconvenience.
"""

import os
import time
import base64
import hashlib
import secrets

from starlette.responses import JSONResponse, RedirectResponse
from starlette.requests import Request

PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
API_KEY = os.environ["MCP_API_KEY"]

_clients: dict[str, dict] = {}
_auth_codes: dict[str, dict] = {}

CODE_TTL_SECONDS = 300


async def protected_resource_metadata(request: Request):
    return JSONResponse({
        "resource": PUBLIC_BASE_URL,
        "authorization_servers": [PUBLIC_BASE_URL],
    })


async def authorization_server_metadata(request: Request):
    return JSONResponse({
        "issuer": PUBLIC_BASE_URL,
        "authorization_endpoint": f"{PUBLIC_BASE_URL}/oauth/authorize",
        "token_endpoint": f"{PUBLIC_BASE_URL}/oauth/token",
        "registration_endpoint": f"{PUBLIC_BASE_URL}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


async def register_client(request: Request):
    body = await request.json()
    client_id = secrets.token_urlsafe(16)
    _clients[client_id] = {
        "redirect_uris": body.get("redirect_uris", []),
    }
    return JSONResponse(
        {
            "client_id": client_id,
            "redirect_uris": _clients[client_id]["redirect_uris"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status_code=201,
    )


async def authorize(request: Request):
    params = request.query_params
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge")

    if not client_id or not redirect_uri or not code_challenge:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    # Single-user server: auto-approve immediately, no login screen.
    code = secrets.token_urlsafe(24)
    _auth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "expires_at": time.time() + CODE_TTL_SECONDS,
    }
    return RedirectResponse(url=f"{redirect_uri}?code={code}&state={state}", status_code=302)


def _pkce_ok(code_verifier: str, code_challenge: str) -> bool:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return computed == code_challenge


async def token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        code = form.get("code")
        code_verifier = form.get("code_verifier", "")
        entry = _auth_codes.pop(code, None)

        if not entry or time.time() > entry["expires_at"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not _pkce_ok(code_verifier, entry["code_challenge"]):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        return JSONResponse({
            "access_token": API_KEY,
            "token_type": "Bearer",
            "expires_in": 31536000,  # our "token" is really the static key, doesn't expire
            "refresh_token": API_KEY,
        })

    if grant_type == "refresh_token":
        # Our access token never actually expires, so refresh just re-issues it.
        return JSONResponse({
            "access_token": API_KEY,
            "token_type": "Bearer",
            "expires_in": 31536000,
        })

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

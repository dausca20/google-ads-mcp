"""Replays Claude's whole sign-in against the server, with a fake Google.

Run by test_sign_in.py in its own process, because the server reads its
sign-in settings when it is first imported. Prints one JSON line with the
status of each step. Pass the scopes the fake Google should grant as
arguments, or none to grant everything that was asked for.
"""

import base64
import hashlib
import json
import logging
import os
import re
import sys
import urllib.parse
from pathlib import Path

os.environ.update(
    GOOGLE_ADS_MCP_OAUTH_CLIENT_ID="123-test.apps.googleusercontent.com",
    GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET="test-secret",
    GOOGLE_ADS_MCP_BASE_URL="http://localhost",
    GOOGLE_ADS_MCP_STORAGE_TYPE="memory",
    GOOGLE_ADS_MCP_JWT_SIGNING_KEY="test-jwt-key",
    ALLOWED_EMAILS="owner@example.com",
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx2  # noqa: E402

CLIENT_ID = os.environ["GOOGLE_ADS_MCP_OAUTH_CLIENT_ID"]
GRANTED = sys.argv[1:]
asked = {"scope": ""}

# Claude's published identity, as served at its client_id URL.
CLAUDE = {
    "client_id": "https://claude.ai/oauth/mcp-oauth-client-metadata",
    "client_name": "Claude",
    "client_uri": "https://claude.ai",
    "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}


def fake_google(request: httpx2.Request) -> httpx2.Response:
    url = str(request.url)
    scope = " ".join(GRANTED) if GRANTED else asked["scope"]
    if url.startswith("https://oauth2.googleapis.com/tokeninfo"):
        return httpx2.Response(200, json={
            "aud": CLIENT_ID, "azp": CLIENT_ID, "sub": "42", "scope": scope,
            "expires_in": "3599", "email": "owner@example.com", "email_verified": "true",
        })
    if url.startswith("https://oauth2.googleapis.com/token"):
        return httpx2.Response(200, json={
            "access_token": "ya29.test", "refresh_token": "1//test", "expires_in": 3599,
            "scope": scope, "token_type": "Bearer",
        })
    if url.startswith("https://www.googleapis.com/oauth2/v2/userinfo"):
        return httpx2.Response(200, json={"email": "owner@example.com", "verified_email": True})
    return httpx2.Response(404, json={"error": f"unexpected request to {url}"})


real_init = httpx2.AsyncClient.__init__


def init_with_fake_google(self, *args, **kwargs):
    kwargs["transport"] = httpx2.MockTransport(fake_google)
    real_init(self, *args, **kwargs)


httpx2.AsyncClient.__init__ = init_with_fake_google

import server  # noqa: E402
from fastmcp.server.auth import cimd  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


async def fetch_claude_identity(self, client_id_url):
    return cimd.CIMDDocument.model_validate(CLAUDE)


cimd.CIMDFetcher.fetch = fetch_claude_identity
warnings = []


class KeepWarnings(logging.Handler):
    def emit(self, record):
        warnings.append(record.getMessage())


logging.getLogger("google_ads_mcp").addHandler(KeepWarnings(level=logging.WARNING))
server.add_email_allowlist()
server.add_sign_in_explanations()

verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
scopes = " ".join(server.mcp.auth._token_validator.required_scopes)
redirect_uri = CLAUDE["redirect_uris"][0]
result = {}

with TestClient(server.mcp.http_app(), base_url="http://localhost", follow_redirects=False) as client:
    response = client.get("/authorize", params={
        "response_type": "code", "client_id": CLAUDE["client_id"], "redirect_uri": redirect_uri,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
        "scope": scopes, "resource": "http://localhost/mcp",
    })
    result["authorize"] = response.status_code
    page = client.get(response.headers["location"]).text
    response = client.post("/consent", data={
        "txn_id": re.search(r'name="txn_id" value="([^"]+)"', page).group(1),
        "csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', page).group(1),
        "submit": "true", "action": "approve",
    })
    to_google = urllib.parse.parse_qs(urllib.parse.urlparse(response.headers["location"]).query)
    asked["scope"] = to_google["scope"][0]
    result["asked_google_for"] = asked["scope"].split()
    response = client.get("/auth/callback", params={"code": "google-code", "state": to_google["state"][0]})
    result["callback"] = response.status_code
    back = urllib.parse.parse_qs(urllib.parse.urlparse(response.headers["location"]).query)
    response = client.post("/token", data={
        "grant_type": "authorization_code", "code": back["code"][0], "redirect_uri": redirect_uri,
        "client_id": CLAUDE["client_id"], "code_verifier": verifier, "resource": "http://localhost/mcp",
    })
    result["token"] = response.status_code
    response = client.post(
        "/mcp",
        headers={
            "authorization": f"Bearer {response.json()['access_token']}",
            "accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        }},
    )
    result["mcp"] = response.status_code

result["warnings"] = warnings
print(json.dumps(result))

"""Runs Google's official Google Ads MCP server with two additions.

Google's server (https://github.com/googleads/google-ads-mcp) does the
reading. This file adds:

1. An email allowlist. When the server runs on the web with Google sign-in
   turned on, only the Google accounts listed in ALLOWED_EMAILS can use it.
2. The change tools in changes.py (preview first, then apply).
3. A home page and privacy policy (pages.py), which Google needs before the
   sign-in app can be published.
"""

import os

from fastmcp.server.auth import AccessToken
from fastmcp.server.middleware import AuthMiddleware
from fastmcp.utilities.authorization import AuthContext

from ads_mcp.coordinator import mcp
from ads_mcp.server import run_server
from changes import changes_mcp
from pages import add_pages

mcp.mount(changes_mcp, namespace="changes")
add_pages(mcp)


def parse_allowed_emails(raw: str | None) -> frozenset[str]:
    """Turns "a@x.com, B@y.com" into {"a@x.com", "b@y.com"}."""
    return frozenset(
        email.strip().lower() for email in (raw or "").split(",") if email.strip()
    )


def token_email_is_allowed(
    token: AccessToken | None, allowed_emails: frozenset[str]
) -> bool:
    """True only for a verified Google email that is on the allowlist."""
    if token is None:
        return False
    claims = token.claims or {}
    email = str(claims.get("email") or "").strip().lower()
    # Google's tokeninfo endpoint returns email_verified as the string "true".
    verified = str(claims.get("email_verified")).lower() == "true"
    return verified and email in allowed_emails


def oauth_is_enabled() -> bool:
    return bool(
        os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID")
        and os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET")
    )


def add_email_allowlist() -> None:
    allowed_emails = parse_allowed_emails(os.environ.get("ALLOWED_EMAILS"))
    if not allowed_emails:
        # Fail closed: a web server with no allowlist would serve anyone.
        raise SystemExit(
            "ALLOWED_EMAILS is empty. Set it to the Google account email(s) "
            "that may use this server, separated by commas."
        )

    def check(ctx: AuthContext) -> bool:
        return token_email_is_allowed(ctx.token, allowed_emails)

    mcp.add_middleware(AuthMiddleware(auth=check))


def main() -> None:
    if oauth_is_enabled():
        add_email_allowlist()
    run_server()


if __name__ == "__main__":
    main()

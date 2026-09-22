"""The two public web pages Google needs before the sign-in app can be published.

Google asks for a home page and a privacy policy link on the Branding page.
These are served by the server itself, at / and /privacy, so the links live
on the same domain as the app. Keep the privacy policy true to what the code
does if the code changes.
"""

from starlette.requests import Request
from starlette.responses import HTMLResponse

LAST_UPDATED = "September 22, 2026"

_STYLE = """
:root { --bg: #ffffff; --text: #1f2328; --muted: #59636e; --link: #0969da; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --text: #e6edf3; --muted: #9198a1; --link: #4493f8; }
}
body { background: var(--bg); color: var(--text); margin: 0;
  font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 680px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 1.75rem; margin: 0 0 8px; }
h2 { font-size: 1.1rem; margin: 28px 0 4px; }
p, li { margin: 6px 0; }
.muted { color: var(--muted); }
a { color: var(--link); }
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{_STYLE}</style>
</head>
<body><main>{body}</main></body>
</html>"""
    )


HOME = """
<h1>Claude Google Ads</h1>
<p>This is a private connector. It lets its owner's Claude assistant read
their own Google Ads accounts. It can also make the changes they ask for,
but only after they review a preview and approve it.</p>
<p>It is not offered to the public. Only the Google accounts on its allowlist
can sign in.</p>
<p><a href="/privacy">Privacy policy</a></p>
"""

PRIVACY = f"""
<h1>Privacy policy</h1>
<p class="muted">Claude Google Ads. Last updated {LAST_UPDATED}.</p>

<h2>What this app can access</h2>
<p>When you sign in with Google, the app asks for permission to use the
Google Ads API for your accounts. It also gets your basic Google profile
(your email and name). Your email is used to check that you are on the
app's allowlist.</p>

<h2>How the data is used</h2>
<p>Only to do what you ask for in Claude: pulling reports and account
settings, and making changes that you preview and approve. The data is not
used for anything else, not used for advertising, and never sold.</p>

<h2>Who it is shared with</h2>
<p>The Google Ads data you ask for is sent to Claude, the AI assistant you
connected. Anthropic runs Claude under the terms of your Claude account.
It is not shared with anyone else.</p>

<h2>What is stored</h2>
<p>The app does not save your Google Ads data. It saves your sign-in tokens,
encrypted, in Google Cloud Firestore in the owner's Google Cloud project, so
you stay connected. Google Cloud's logs may record which report queries were
run, but not their results.</p>

<h2>How to remove access</h2>
<p>Disconnect the connector in Claude, or remove the app on your
<a href="https://myaccount.google.com/permissions">Google Account
third-party access page</a>.</p>

<h2>Google API data</h2>
<p>This app's use of information received from Google APIs follows the
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google
API Services User Data Policy</a>, including the Limited Use requirements.</p>
"""


def add_pages(mcp) -> None:
    @mcp.custom_route("/", methods=["GET"])
    async def home(request: Request) -> HTMLResponse:
        return _page("Claude Google Ads", HOME)

    @mcp.custom_route("/privacy", methods=["GET"])
    async def privacy(request: Request) -> HTMLResponse:
        return _page("Privacy policy", PRIVACY)

"""Checks Claude's whole sign-in flow, end to end, against a fake Google."""

import json
import subprocess
import sys
from pathlib import Path

FLOW = Path(__file__).with_name("sign_in_flow.py")
ADS = "https://www.googleapis.com/auth/adwords"
BASIC = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def run_flow(*granted):
    done = subprocess.run(
        [sys.executable, str(FLOW), *granted],
        capture_output=True, text=True, timeout=120, check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_claude_can_sign_in_and_use_the_server():
    result = run_flow()
    assert ADS in result["asked_google_for"]
    assert (result["authorize"], result["callback"], result["token"]) == (302, 302, 200)
    assert result["mcp"] == 200
    assert result["warnings"] == []


def test_sign_in_without_google_ads_permission_is_explained():
    result = run_flow(*BASIC)
    assert result["token"] == 200
    assert result["mcp"] == 401
    assert any(f"missing permission for: {ADS}" in w for w in result["warnings"])

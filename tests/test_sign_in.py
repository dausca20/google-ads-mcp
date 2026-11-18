"""Checks Claude's whole sign-in flow, end to end, against a fake Google."""

import json
import os
import subprocess
import sys
from pathlib import Path

FLOW = Path(__file__).with_name("sign_in_flow.py")
ADS = "https://www.googleapis.com/auth/adwords"
TAG_MANAGER = [
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.edit.containerversions",
    "https://www.googleapis.com/auth/tagmanager.publish",
]
BASIC = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def run_flow(*granted, google_refuses_renewal=False):
    env = dict(os.environ)
    if google_refuses_renewal:
        env["FAKE_GOOGLE_REFUSES_RENEWAL"] = "1"
    done = subprocess.run(
        [sys.executable, str(FLOW), *granted],
        capture_output=True, text=True, timeout=120, check=True, env=env,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_claude_can_sign_in_and_use_the_server():
    result = run_flow()
    assert ADS in result["asked_google_for"]
    assert set(TAG_MANAGER) <= set(result["asked_google_for"])
    assert (result["authorize"], result["callback"], result["token"]) == (302, 302, 200)
    assert result["mcp"] == 200
    assert result["after_restart"] == 200
    assert result["warnings"] == []


def test_claude_stays_connected_the_next_day():
    result = run_flow()
    # Claude doesn't have to renew anything for a year...
    assert result["claude_sign_in_seconds"] >= 364 * 24 * 60 * 60
    # ...because the server renews Google's hourly sign-in by itself, once.
    assert (result["next_day"], result["next_day_again"]) == (200, 200)
    assert result["google_renewals"] == 1
    assert result["warnings"] == []


def test_refused_renewal_is_explained():
    result = run_flow(google_refuses_renewal=True)
    assert result["mcp"] == 200
    assert result["next_day"] == 401
    assert any("wouldn't renew" in w and "invalid_grant" in w for w in result["warnings"])


def test_sign_in_without_google_ads_permission_is_explained():
    result = run_flow(*BASIC)
    assert result["token"] == 200
    assert result["mcp"] == 401
    assert any(f"missing permission for: {ADS}" in w for w in result["warnings"])


def test_sign_in_from_before_tag_manager_is_explained():
    result = run_flow(*BASIC, ADS)
    assert result["mcp"] == 401
    assert any(f"missing permission for: {', '.join(TAG_MANAGER)}" in w for w in result["warnings"])

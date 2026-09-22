"""Checks that only allowlisted, verified Google emails can use the server."""

import asyncio
from unittest import mock

import pytest
from fastmcp import Client
from fastmcp.server.auth import AccessToken

import server
from ads_mcp.coordinator import mcp

ALLOWED = server.parse_allowed_emails("Owner@Example.com, helper@example.com")


def make_token(email, verified="true"):
    return AccessToken(
        token="t",
        client_id="c",
        scopes=[],
        claims={"email": email, "email_verified": verified},
    )


def test_parse_allowed_emails_trims_and_lowercases():
    assert ALLOWED == {"owner@example.com", "helper@example.com"}
    assert server.parse_allowed_emails(" , ") == frozenset()
    assert server.parse_allowed_emails(None) == frozenset()


@pytest.mark.parametrize(
    "token, expected",
    [
        (make_token("owner@example.com"), True),
        (make_token("OWNER@example.com"), True),
        (make_token("helper@example.com", verified=True), True),
        (make_token("stranger@example.com"), False),
        (make_token("owner@example.com", verified="false"), False),
        (make_token("owner@example.com", verified=None), False),
        (make_token(None), False),
        (None, False),
    ],
)
def test_token_email_is_allowed(token, expected):
    assert server.token_email_is_allowed(token, ALLOWED) is expected


def test_empty_allowlist_refuses_to_start(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "")
    with pytest.raises(SystemExit):
        server.add_email_allowlist()


def list_tool_names_as(token):
    async def run():
        async with Client(mcp) as client:
            return sorted(tool.name for tool in await client.list_tools())

    with mock.patch(
        "fastmcp.server.middleware.authorization.get_access_token",
        return_value=token,
    ):
        return asyncio.run(run())


@pytest.fixture
def allowlisted_server(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "owner@example.com")
    saved = list(mcp.middleware)
    server.add_email_allowlist()
    yield
    mcp.middleware[:] = saved


def test_allowed_user_sees_google_ads_tools(allowlisted_server):
    assert list_tool_names_as(make_token("owner@example.com")) == [
        "changes_apply_change",
        "changes_preview_bid_adjustments",
        "changes_preview_negative_keywords",
        "changes_preview_new_search_ad",
        "changes_preview_status_change",
        "customers_list_accessible_customers",
        "metadata_get_resource_metadata",
        "search_search",
    ]


def test_other_user_sees_no_tools(allowlisted_server):
    assert list_tool_names_as(make_token("stranger@example.com")) == []


def test_other_user_cannot_run_search(allowlisted_server):
    async def run():
        async with Client(mcp) as client:
            await client.call_tool(
                "search_search",
                {"customer_id": "1", "fields": ["campaign.id"], "resource": "campaign"},
            )

    with mock.patch(
        "fastmcp.server.middleware.authorization.get_access_token",
        return_value=make_token("stranger@example.com"),
    ):
        with pytest.raises(Exception, match="Authorization failed"):
            asyncio.run(run())


def test_home_and_privacy_pages_are_public():
    from starlette.testclient import TestClient

    client = TestClient(mcp.http_app())
    home = client.get("/")
    assert home.status_code == 200
    assert 'href="/privacy"' in home.text
    privacy = client.get("/privacy")
    assert privacy.status_code == 200
    assert "Limited Use" in privacy.text

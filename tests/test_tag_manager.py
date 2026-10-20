"""Checks the Tag Manager tools against a fake Tag Manager API.

The fake records every request, so these tests prove what would be sent,
that previews never add or publish anything, and that apply_change sends
exactly what was previewed.
"""

import copy

import httpx2
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken

import changes
import tag_manager as tm

CONTAINER = "accounts/1/containers/2"
WORKSPACE = f"{CONTAINER}/workspaces/3"


class FakeTagManager:
    """Stands in for the Tag Manager API: canned answers, recorded requests."""

    def __init__(self):
        self.calls = []
        self.fail_on = None
        self.workspaces = [{
            "workspaceId": "3", "name": "Default Workspace", "path": WORKSPACE,
            "tagManagerUrl": "https://tagmanager.google.com/ws/3",
        }]
        self.tags = [{
            "tagId": "1", "name": "Conversion Linker", "type": "gclidw", "path": f"{WORKSPACE}/tags/1",
            "fingerprint": "f1", "firingTriggerId": ["2147479553"],
        }]
        self.triggers = [{"triggerId": "7", "name": "Thank You Page", "type": "pageview", "path": f"{WORKSPACE}/triggers/7"}]
        self.variables = []
        self.built_ins = [{"type": "pageUrl", "name": "Page URL"}]
        self.status = {"workspaceChange": [
            {"changeStatus": "added", "tag": {"name": "Google Ads - Form", "path": f"{WORKSPACE}/tags/9", "fingerprint": "a"}},
            {"changeStatus": "updated", "trigger": {"name": "Thank You Page", "path": f"{WORKSPACE}/triggers/7", "fingerprint": "b"}},
        ]}
        self.quick_preview = {"compilerError": False}
        self.version = {
            "containerVersionId": "42", "name": "Add form conversion", "path": f"{CONTAINER}/versions/42",
            "fingerprint": "v42", "tagManagerUrl": "https://tagmanager.google.com/v/42",
        }

    def __call__(self, method, path, params=None, body=None):
        self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
        if self.fail_on and self.fail_on in (method, path):
            raise ToolError("Tag Manager said no (HTTP 400): bad request")
        routes = {
            ("GET", "accounts"): {"account": [{"accountId": "1", "name": "Client"}]},
            ("GET", "accounts/1/containers"): {"container": [{"containerId": "2", "name": "Site", "publicId": "GTM-ABC"}]},
            ("GET", CONTAINER): {"containerId": "2", "name": "Site", "publicId": "GTM-ABC"},
            ("GET", f"{CONTAINER}/workspaces"): {"workspace": self.workspaces},
            ("GET", WORKSPACE): self.workspaces[0],
            ("GET", f"{CONTAINER}/versions:live"): {"containerVersionId": "41", "name": "Launch", "tag": self.tags, "trigger": self.triggers},
            ("GET", f"{WORKSPACE}/tags"): {"tag": self.tags},
            ("GET", f"{WORKSPACE}/triggers"): {"trigger": self.triggers},
            ("GET", f"{WORKSPACE}/variables"): {"variable": self.variables},
            ("GET", f"{WORKSPACE}/built_in_variables"): {"builtInVariable": self.built_ins},
            ("GET", f"{WORKSPACE}/status"): self.status,
            ("POST", f"{WORKSPACE}:quick_preview"): self.quick_preview,
            ("POST", f"{WORKSPACE}/built_in_variables"): {"builtInVariable": []},
            ("POST", f"{WORKSPACE}/variables"): {"variableId": "30"},
            ("POST", f"{WORKSPACE}/tags"): {"tagId": "31"},
            ("POST", f"{WORKSPACE}:create_version"): {
                "containerVersion": self.version, "newWorkspacePath": f"{CONTAINER}/workspaces/4",
            },
            ("POST", f"{CONTAINER}/versions/42:publish"): {"containerVersion": self.version},
        }
        if (method, path) == ("POST", f"{WORKSPACE}/triggers"):
            return {"triggerId": str(20 + sum(1 for c in self.calls if c[:2] == (method, path)) - 1)}
        return routes[(method, path)]

    def writes(self):
        return [(method, path) for method, path, _, _ in self.calls if method != "GET" and not path.endswith(":quick_preview")]


@pytest.fixture
def gtm(monkeypatch):
    fake = FakeTagManager()
    monkeypatch.setattr(tm, "_call", fake)
    token = AccessToken(token="t", client_id="c", scopes=[], claims={"email": "owner@example.com", "email_verified": "true"})
    monkeypatch.setattr(changes, "get_access_token", lambda: token)
    monkeypatch.setenv("GOOGLE_ADS_MCP_JWT_SIGNING_KEY", "test-key")
    changes._used_nonces.clear()
    return fake


def form_conversion(**overrides):
    tag = {
        "name": "Google Ads - Contact Form",
        "type": "awct",
        "parameter": [
            {"type": "template", "key": "conversionId", "value": "123456789"},
            {"type": "template", "key": "conversionLabel", "value": "AbC-1"},
        ],
        "firing_triggers": ["Contact Form Submit", "thank you page"],
    }
    return tm.NewTag(**{**tag, **overrides})


def contact_form_trigger():
    return tm.NewTrigger(
        name="Contact Form Submit",
        type="formSubmission",
        filter=[{"type": "equals", "parameter": [
            {"type": "template", "key": "arg0", "value": "{{Form ID}}"},
            {"type": "template", "key": "arg1", "value": "contact"},
        ]}],
        check_validation=True,
    )


# --- Reading ----------------------------------------------------------------


def test_list_containers(gtm):
    assert tm.list_containers() == [{
        "account_id": "1", "account_name": "Client",
        "containers": [{"container_id": "2", "name": "Site", "public_id": "GTM-ABC", "usage": [], "domains": []}],
    }]


def test_get_workspace_names_triggers_and_unpublished_changes(gtm):
    result = tm.get_workspace(account_id="1", container_id="2")
    assert result["workspace"]["workspace_id"] == "3"
    assert result["tags"] == [{"tagId": "1", "name": "Conversion Linker", "type": "gclidw", "firing_triggers": ["All Pages (2147479553)"]}]
    assert result["unpublished_changes"] == ['Added tag "Google Ads - Form"', 'Changed trigger "Thank You Page"']
    assert result["built_in_variables"] == ["Page URL (pageUrl)"]
    assert gtm.writes() == []


def test_several_workspaces_need_a_workspace_id(gtm):
    gtm.workspaces.append({"workspaceId": "5", "name": "Claude", "path": f"{CONTAINER}/workspaces/5"})
    with pytest.raises(ToolError, match=r'2 workspaces.*"Default Workspace" \(3\); "Claude" \(5\)'):
        tm.get_workspace(account_id="1", container_id="2")


# --- Adding -----------------------------------------------------------------


def test_preview_new_items_only_reads(gtm):
    preview = tm.preview_new_items(
        account_id="1",
        container_id="2",
        tags=[form_conversion(), tm.NewTag(name="conversion linker", type="gclidw", firing_triggers=["All Pages"])],
        triggers=[contact_form_trigger()],
        variables=[tm.NewVariable(name="Lead Value", type="c", parameter=[{"type": "template", "key": "value", "value": "50"}])],
        built_in_variables=["formId", "pageUrl", "formId"],
    )
    assert preview["preview"] == 'Add 4 item(s) to workspace "Default Workspace" in container "Site" (GTM-ABC)'
    assert preview["changes"] == [
        "Turn on built-in variable formId",
        'Variable "Lead Value" (type c); settings: value=50',
        'Trigger "Contact Form Submit" (formSubmission) when {{Form ID}} equals contact',
        'Tag "Google Ads - Contact Form" (awct), fires on "Contact Form Submit" (new), "Thank You Page" (7); '
        "settings: conversionId=123456789, conversionLabel=AbC-1",
    ]
    assert preview["skipped"] == [
        "Built-in variable pageUrl: already turned on",
        "Built-in variable formId: listed twice",
        'Tag "conversion linker": already in this workspace',
    ]
    assert preview["change_code"]
    assert gtm.writes() == []


def test_apply_adds_exactly_what_was_previewed(gtm):
    code = tm.preview_new_items(
        account_id="1", container_id="2",
        tags=[form_conversion(paused=True, firing_option="oncePerEvent")],
        triggers=[contact_form_trigger()],
        built_in_variables=["formId"],
    )["change_code"]
    gtm.calls.clear()
    result = changes.apply_change(code)
    writes = [(m, p, params, body) for m, p, params, body in gtm.calls if m == "POST"]
    assert [(m, p) for m, p, _, _ in writes] == [
        ("POST", f"{WORKSPACE}/built_in_variables"),
        ("POST", f"{WORKSPACE}/triggers"),
        ("POST", f"{WORKSPACE}/tags"),
    ]
    assert writes[0][2] == [("type", "formId")]
    assert writes[1][3] == {
        "name": "Contact Form Submit",
        "type": "formSubmission",
        "filter": [{"type": "equals", "parameter": [
            {"type": "template", "key": "arg0", "value": "{{Form ID}}"},
            {"type": "template", "key": "arg1", "value": "contact"},
        ]}],
        "checkValidation": {"type": "boolean", "value": "true"},
    }
    assert writes[2][3] == {
        "name": "Google Ads - Contact Form",
        "type": "awct",
        "parameter": [
            {"type": "template", "key": "conversionId", "value": "123456789"},
            {"type": "template", "key": "conversionLabel", "value": "AbC-1"},
        ],
        "paused": True,
        "tagFiringOption": "oncePerEvent",
        "firingTriggerId": ["20", "7"],
    }
    assert result["items_changed"] == 3
    assert "Nothing is live yet" in result["note"]
    with pytest.raises(ToolError, match="already applied"):
        changes.apply_change(code)


def test_list_and_map_parameters_keep_tag_managers_names(gtm):
    code = tm.preview_new_items(account_id="1", container_id="2", tags=[form_conversion(
        firing_triggers=["2147479553"],
        parameter=[{"type": "list", "key": "rows", "list": [{"type": "map", "map": [{"type": "template", "key": "k", "value": "v"}]}]}],
    )])["change_code"]
    changes.apply_change(code)
    [body] = [body for m, p, _, body in gtm.calls if (m, p) == ("POST", f"{WORKSPACE}/tags")]
    assert body["parameter"] == [{"type": "list", "key": "rows", "list": [{"type": "map", "map": [{"type": "template", "key": "k", "value": "v"}]}]}]
    assert body["firingTriggerId"] == ["2147479553"]


def test_values_are_sent_as_text():
    parameters = [
        tm.Parameter(type="boolean", key="enabled", value=True),
        tm.Parameter(type="integer", key="timeout", value=2000),
        tm.Parameter(type="template", key="price", value=12.5),
    ]
    assert [p["value"] for p in tm._dump(parameters)] == ["true", "2000", "12.5"]


def test_unknown_trigger_is_refused(gtm):
    with pytest.raises(ToolError, match='no trigger named or numbered "Nope"'):
        tm.preview_new_items(account_id="1", container_id="2", tags=[form_conversion(firing_triggers=["Nope"])])


def test_nothing_new_is_refused(gtm):
    with pytest.raises(ToolError, match="Nothing to add"):
        tm.preview_new_items(account_id="1", container_id="2", tags=[tm.NewTag(name="Conversion Linker", type="gclidw", firing_triggers=["All Pages"])])


def test_a_failed_add_says_what_was_and_wasnt_added(gtm):
    code = tm.preview_new_items(
        account_id="1", container_id="2", tags=[form_conversion()], triggers=[contact_form_trigger()],
    )["change_code"]
    gtm.fail_on = f"{WORKSPACE}/tags"
    with pytest.raises(ToolError) as failure:
        changes.apply_change(code)
    message = str(failure.value)
    assert 'stopped at tag "Google Ads - Contact Form"' in message
    assert 'Added before that, in the workspace and not live: trigger "Contact Form Submit".' in message
    assert 'Not added: tag "Google Ads - Contact Form".' in message
    # Used up, so a retry can't add the trigger twice.
    with pytest.raises(ToolError, match="already applied"):
        changes.apply_change(code)


# --- Publishing -------------------------------------------------------------


def test_preview_publish_checks_without_publishing(gtm):
    preview = tm.preview_publish(account_id="1", container_id="2", version_name=" Add form   conversion ")
    assert preview["preview"] == 'Publish workspace "Default Workspace" of container "Site" (GTM-ABC) as version "Add form conversion"'
    assert preview["changes"] == [
        'Added tag "Google Ads - Form"',
        'Changed trigger "Thank You Page"',
        'Replaces live version 41 ("Launch")',
    ]
    assert preview["warnings"] == ["Publishing changes what runs on the live website as soon as it's applied."]
    assert ("POST", f"{WORKSPACE}:quick_preview") in [(m, p) for m, p, _, _ in gtm.calls]
    assert gtm.writes() == []


def test_apply_publish_saves_a_version_and_publishes_it(gtm):
    code = tm.preview_publish(account_id="1", container_id="2", version_name="Add form conversion", version_notes="From Claude")["change_code"]
    gtm.calls.clear()
    result = changes.apply_change(code)
    posts = [(p, params, body) for m, p, params, body in gtm.calls if m == "POST"]
    assert posts == [
        (f"{WORKSPACE}:create_version", None, {"name": "Add form conversion", "notes": "From Claude"}),
        (f"{CONTAINER}/versions/42:publish", {"fingerprint": "v42"}, None),
    ]
    assert result["version_id"] == "42"
    assert result["new_workspace_id"] == "4"


def test_publish_is_refused_when_the_workspace_changed(gtm):
    code = tm.preview_publish(account_id="1", container_id="2", version_name="v")["change_code"]
    gtm.status["workspaceChange"][0]["tag"]["fingerprint"] = "edited"
    with pytest.raises(ToolError, match="changed since the preview"):
        changes.apply_change(code)
    assert gtm.writes() == []


def test_publish_preview_stops_on_errors_and_conflicts(gtm):
    gtm.quick_preview["compilerError"] = True
    with pytest.raises(ToolError, match="found errors"):
        tm.preview_publish(account_id="1", container_id="2", version_name="v")
    gtm.quick_preview.update(compilerError=False, syncStatus={"mergeConflict": True})
    with pytest.raises(ToolError, match="conflicts"):
        tm.preview_publish(account_id="1", container_id="2", version_name="v")


def test_nothing_to_publish(gtm):
    gtm.status["workspaceChange"] = []
    with pytest.raises(ToolError, match="Nothing to publish"):
        tm.preview_publish(account_id="1", container_id="2", version_name="v")


def test_a_failed_publish_says_the_version_was_saved(gtm):
    code = tm.preview_publish(account_id="1", container_id="2", version_name="v")["change_code"]
    gtm.fail_on = f"{CONTAINER}/versions/42:publish"
    with pytest.raises(ToolError, match=r'Saved version 42 \("Add form conversion"\) but didn\'t publish it'):
        changes.apply_change(code)


# --- The HTTP request itself -------------------------------------------------


def test_calls_send_the_google_sign_in_and_explain_errors(monkeypatch):
    seen = []

    def fake_api(request):
        seen.append(request)
        if request.url.path.endswith("/denied"):
            return httpx2.Response(403, json={"error": {"code": 403, "message": "Tag Manager API has not been used in project 1"}})
        return httpx2.Response(200, json={"account": []})

    real_init = httpx2.Client.__init__
    monkeypatch.setattr(
        httpx2.Client, "__init__",
        lambda self, *a, **k: real_init(self, *a, **{**k, "transport": httpx2.MockTransport(fake_api)}),
    )
    monkeypatch.setattr(tm, "get_access_token", lambda: AccessToken(token="ya29.user", client_id="c", scopes=[]))
    assert tm._call("GET", "accounts", params={"pageToken": "p2"}) == {"account": []}
    assert str(seen[0].url) == "https://tagmanager.googleapis.com/tagmanager/v2/accounts?pageToken=p2"
    assert seen[0].headers["authorization"] == "Bearer ya29.user"
    with pytest.raises(ToolError, match=r"Tag Manager said no \(HTTP 403\): Tag Manager API has not been used"):
        tm._call("GET", "accounts/denied")

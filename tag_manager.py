"""Tools that let Claude read Google Tag Manager, add to a workspace, and publish.

Reading needs no preview. Adding tags, triggers, and variables, and publishing
a workspace, take the same two steps as the Google Ads changes in changes.py:
a preview tool returns a change code, and apply_change makes the change.
Nothing reaches the website until a publish is applied.

Calls go to Tag Manager's REST API (v2) with the Google sign-in Claude
already uses for Google Ads:
https://developers.google.com/tag-platform/tag-manager/api/reference/rest
"""

import hashlib
import json
from typing import Any, Literal

import httpx2
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator

from changes import OTHER_APPLIERS, PREVIEW, _id, preview_result

tag_manager_mcp = FastMCP("tag_manager")

# Every call here accepts one of these: edit.containers reads and adds,
# edit.containerversions builds versions, and publish publishes them.
TAG_MANAGER_SCOPES = [
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.edit.containerversions",
    "https://www.googleapis.com/auth/tagmanager.publish",
]

API = "https://tagmanager.googleapis.com/tagmanager/v2/"
MAX_NEW_ITEMS = 25
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

# Tag Manager's own triggers. Tags can fire on them, but they aren't listed
# with the workspace's triggers.
BUILT_IN_TRIGGERS = {
    "2147479553": "All Pages",
    "2147479572": "Initialization - All Pages",
    "2147479573": "Consent Initialization - All Pages",
}

# Fields that only locate an item, left out of what Claude reads.
LOCATION_FIELDS = {"accountId", "containerId", "workspaceId", "path", "fingerprint", "tagManagerUrl"}

ENTITY_LABELS = {
    "tag": "tag",
    "trigger": "trigger",
    "variable": "variable",
    "builtInVariable": "built-in variable",
    "folder": "folder",
    "customTemplate": "template",
    "gtagConfig": "Google tag setting",
    "client": "client",
    "zone": "zone",
    "transformation": "transformation",
}


# --- Talking to Tag Manager -------------------------------------------------


def _google_token() -> str:
    token = get_access_token()
    if token is None or not token.token:
        raise ToolError("Tag Manager needs the Google sign-in from Claude. Reconnect the connector in Claude.")
    return token.token


def _google_error(response) -> str:
    try:
        message = response.json()["error"]["message"]
    except Exception:
        message = response.text[:500]
    return f"Tag Manager said no (HTTP {response.status_code}): {message}"


def _call(method: str, path: str, params=None, body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {_google_token()}"}
    try:
        with httpx2.Client(timeout=60) as client:
            response = client.request(method, API + path, params=params, json=body, headers=headers)
    except httpx2.HTTPError as ex:
        raise ToolError(f"Couldn't reach Tag Manager: {ex}") from ex
    if response.status_code >= 400:
        raise ToolError(_google_error(response))
    return response.json() if response.content else {}


def _list(path: str, field: str) -> list[dict]:
    """Every item of a list, across all of its pages."""
    items, page = [], None
    while True:
        data = _call("GET", path, params={"pageToken": page} if page else None)
        items += data.get(field, [])
        page = data.get("nextPageToken")
        if not page:
            return items


def _container_path(account_id: str | int, container_id: str | int) -> str:
    return f"accounts/{_id(account_id, 'account_id')}/containers/{_id(container_id, 'container_id')}"


def _workspace(account_id, container_id, workspace_id) -> dict:
    """The workspace to use. Without an ID, the container's only workspace."""
    container = _container_path(account_id, container_id)
    if workspace_id not in (None, ""):
        return _call("GET", f"{container}/workspaces/{_id(workspace_id, 'workspace_id')}")
    workspaces = _list(f"{container}/workspaces", "workspace")
    if len(workspaces) == 1:
        return workspaces[0]
    names = "; ".join(f"\"{w.get('name')}\" ({w.get('workspaceId')})" for w in workspaces)
    raise ToolError(f"This container has {len(workspaces)} workspaces. Give workspace_id for one of: {names}")


def _contents(workspace_path: str) -> dict[str, list[dict]]:
    return {
        "tags": _list(f"{workspace_path}/tags", "tag"),
        "triggers": _list(f"{workspace_path}/triggers", "trigger"),
        "variables": _list(f"{workspace_path}/variables", "variable"),
        "built_in_variables": _list(f"{workspace_path}/built_in_variables", "builtInVariable"),
    }


# --- Making it readable -----------------------------------------------------


def _key(name: str) -> str:
    return " ".join(str(name).split()).lower()


def _without_location(item: dict) -> dict:
    return {key: value for key, value in item.items() if key not in LOCATION_FIELDS}


def _readable(contents: dict[str, list[dict]]) -> dict[str, Any]:
    names = {**BUILT_IN_TRIGGERS, **{t["triggerId"]: t.get("name", "") for t in contents["triggers"]}}
    tags = []
    for tag in contents["tags"]:
        cleaned = _without_location(tag)
        for field, readable in (("firingTriggerId", "firing_triggers"), ("blockingTriggerId", "blocking_triggers")):
            ids = cleaned.pop(field, [])
            if ids:
                cleaned[readable] = [f"{names.get(i, 'Unknown trigger')} ({i})" for i in ids]
        tags.append(cleaned)
    return {
        "tags": tags,
        "triggers": [_without_location(t) for t in contents["triggers"]],
        "variables": [_without_location(v) for v in contents["variables"]],
        "built_in_variables": [f"{b.get('name')} ({b.get('type')})" for b in contents["built_in_variables"]],
    }


def _change_entities(status: dict) -> list[tuple[str, str, dict]]:
    """(change status, kind, item) for each change in a workspace."""
    found = []
    for change in status.get("workspaceChange", []):
        kind = next((k for k in change if k != "changeStatus"), "")
        found.append((change.get("changeStatus", ""), kind, change.get(kind, {})))
    return found


def _change_text(change_status: str, kind: str, item: dict) -> str:
    verb = {"added": "Added", "deleted": "Deleted", "updated": "Changed"}.get(change_status, "Changed")
    return f"{verb} {ENTITY_LABELS.get(kind, kind)} \"{item.get('name') or item.get('type', '')}\""


def _status_key(status: dict) -> str:
    """Changes whenever anything in the workspace's unpublished changes does."""
    rows = sorted(
        [change_status, kind, item.get("path", ""), item.get("fingerprint", "")]
        for change_status, kind, item in _change_entities(status)
    )
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _settings_text(parameters: list[dict]) -> str:
    parts = []
    for p in parameters:
        if p.get("type") in ("list", "map"):
            parts.append(f"{p.get('key')}: {len(p.get(p['type']) or [])} item(s)")
        else:
            parts.append(f"{p.get('key')}={p.get('value', '')}")
    return f"; settings: {', '.join(parts)}" if parts else ""


def _condition_text(condition: dict) -> str:
    args = {p.get("key"): p.get("value") for p in condition.get("parameter", [])}
    negate = "not " if args.get("negate") == "true" else ""
    return f"{args.get('arg0', '?')} {negate}{condition['type']} {args.get('arg1', '')}".strip()


# --- Reading ----------------------------------------------------------------


@tag_manager_mcp.tool(annotations=READ)
def list_containers() -> list[dict[str, Any]]:
    """Lists the Google Tag Manager accounts and containers the user can access.

    account_id and container_id are the numbers the other Tag Manager tools
    need. public_id (GTM-XXXX) is the one in the website's code.
    """
    result = []
    for account in _list("accounts", "account"):
        containers = _list(f"accounts/{account['accountId']}/containers", "container")
        result.append({
            "account_id": account["accountId"],
            "account_name": account.get("name"),
            "containers": [
                {
                    "container_id": c["containerId"],
                    "name": c.get("name"),
                    "public_id": c.get("publicId"),
                    "usage": c.get("usageContext", []),
                    "domains": c.get("domainName", []),
                }
                for c in containers
            ],
        })
    return result


@tag_manager_mcp.tool(annotations=READ)
def list_workspaces(account_id: str | int, container_id: str | int) -> dict[str, Any]:
    """Lists a container's workspaces and names the version that is live on the website."""
    container = _container_path(account_id, container_id)
    workspaces = _list(f"{container}/workspaces", "workspace")
    try:
        live = _call("GET", f"{container}/versions:live")
        live_version = {"version_id": live.get("containerVersionId"), "name": live.get("name", "")}
    except ToolError as ex:
        live_version = f"Couldn't read the live version. {ex}"
    return {
        "workspaces": [
            {"workspace_id": w["workspaceId"], "name": w.get("name"), "description": w.get("description", "")}
            for w in workspaces
        ],
        "live_version": live_version,
    }


@tag_manager_mcp.tool(annotations=READ)
def get_workspace(
    account_id: str | int,
    container_id: str | int,
    workspace_id: str | int | None = None,
) -> dict[str, Any]:
    """Shows a workspace's tags, triggers, and variables, and its changes that aren't live yet.

    workspace_id can be left out when the container has one workspace. Tags
    list their triggers as "Name (ID)". Parameters are in Tag Manager's API
    format, the same format tag_manager_preview_new_items takes.
    """
    workspace = _workspace(account_id, container_id, workspace_id)
    path = workspace["path"]
    status = _call("GET", f"{path}/status")
    return {
        "workspace": {
            "workspace_id": workspace["workspaceId"],
            "name": workspace.get("name"),
            "url": workspace.get("tagManagerUrl"),
        },
        "unpublished_changes": [_change_text(*change) for change in _change_entities(status)],
        "merge_conflicts": len(status.get("mergeConflict", [])),
        **_readable(_contents(path)),
    }


@tag_manager_mcp.tool(annotations=READ)
def get_live_version(account_id: str | int, container_id: str | int) -> dict[str, Any]:
    """Shows what is on the website now: the live version's tags, triggers, and variables."""
    live = _call("GET", f"{_container_path(account_id, container_id)}/versions:live")
    return {
        "version_id": live.get("containerVersionId"),
        "name": live.get("name", ""),
        "description": live.get("description", ""),
        "url": live.get("tagManagerUrl"),
        **_readable({
            "tags": live.get("tag", []),
            "triggers": live.get("trigger", []),
            "variables": live.get("variable", []),
            "built_in_variables": live.get("builtInVariable", []),
        }),
    }


# --- Adding to a workspace --------------------------------------------------


class Parameter(BaseModel):
    """A setting in Tag Manager's API format, for example
    {"type": "template", "key": "conversionId", "value": "123456789"}."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    type: Literal["template", "boolean", "integer", "list", "map", "triggerReference", "tagReference"]
    key: str | None = Field(default=None, description="Required, except for items inside a list.")
    value: str | None = Field(
        default=None,
        description="Text, 'true' or 'false', or a number, as text. Can use variables like {{Page URL}}.",
    )
    items: list["Parameter"] | None = Field(default=None, alias="list", description="The items, for type list.")
    entries: list["Parameter"] | None = Field(default=None, alias="map", description="The entries, for type map.")

    @field_validator("value", mode="before")
    @classmethod
    def value_as_text(cls, value):
        # Tag Manager wants every value as text: true becomes "true", 50 "50".
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (int, float)):
            return str(value)
        return value


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal[
        "equals", "contains", "startsWith", "endsWith", "matchRegex", "greater",
        "greaterOrEquals", "less", "lessOrEquals", "cssSelector", "urlMatches",
    ]
    parameter: list[Parameter] = Field(
        description="arg0 is a variable like {{Page Path}}, arg1 is the value to compare. "
        "A boolean 'negate' set to 'true' flips it. For matchRegex, a boolean 'ignore_case' "
        "set to 'true' ignores case."
    )


TriggerType = Literal[
    "pageview", "domReady", "windowLoaded", "customEvent", "triggerGroup", "init", "consentInit",
    "formSubmission", "click", "linkClick", "jsError", "historyChange", "timer", "youTubeVideo",
    "scrollDepth", "elementVisibility",
]

BuiltInVariableType = Literal[
    "pageUrl", "pageHostname", "pagePath", "referrer", "event", "clickElement", "clickClasses",
    "clickId", "clickTarget", "clickUrl", "clickText", "formElement", "formClasses", "formId",
    "formTarget", "formUrl", "formText", "errorMessage", "errorUrl", "errorLine", "newHistoryUrl",
    "oldHistoryUrl", "newHistoryFragment", "oldHistoryFragment", "newHistoryState",
    "oldHistoryState", "historySource", "containerVersion", "debugMode", "randomNumber",
    "containerId", "htmlId", "environmentName", "videoProvider", "videoUrl", "videoTitle",
    "videoDuration", "videoPercent", "videoVisible", "videoStatus", "videoCurrentTime",
    "scrollDepthThreshold", "scrollDepthUnits", "scrollDepthDirection", "elementVisibilityRatio",
    "elementVisibilityTime", "elementVisibilityFirstTime", "elementVisibilityRecentTime",
]


class NewVariable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: str = Field(description="Tag Manager's code for the variable type. Copy it from a similar variable if there is one.")
    parameter: list[Parameter] = []
    notes: str | None = None


class NewTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: TriggerType
    filter: list[Condition] = Field(default=[], description="The trigger only fires when all of these are true.")
    custom_event_filter: list[Condition] = Field(
        default=[], description="For customEvent triggers: the event fires it only when all of these are true."
    )
    auto_event_filter: list[Condition] = Field(default=[], description="For auto event tracking (form, click, and link triggers).")
    parameter: list[Parameter] = Field(default=[], description="Other settings, in Tag Manager's API format.")
    wait_for_tags: bool | None = Field(
        default=None, description="formSubmission and linkClick only: hold the form submit or link until tags fire."
    )
    check_validation: bool | None = Field(
        default=None,
        description="formSubmission and linkClick only: fire only when the submit or click isn't cancelled, for example by form validation.",
    )
    notes: str | None = None


class NewTag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: str = Field(
        description="Tag Manager's code for the tag type. Copy it and the parameter keys from a tag of the "
        "same kind in tag_manager_get_workspace or tag_manager_get_live_version if there is one."
    )
    parameter: list[Parameter] = []
    firing_triggers: list[str] = Field(
        description="Names or IDs of the triggers that fire this tag. Can name a trigger added in this "
        "same preview, or All Pages, Initialization - All Pages, or Consent Initialization - All Pages."
    )
    blocking_triggers: list[str] = Field(default=[], description="Names or IDs of triggers that stop this tag.")
    firing_option: Literal["unlimited", "oncePerEvent", "oncePerLoad"] | None = Field(
        default=None, description="How often it can fire. Leave empty for Tag Manager's default."
    )
    paused: bool = False
    notes: str | None = None


def _dump(parameters: list[Parameter]) -> list[dict]:
    return [p.model_dump(by_alias=True, exclude_none=True) for p in parameters]


@tag_manager_mcp.tool(annotations=PREVIEW)
def preview_new_items(
    account_id: str | int,
    container_id: str | int,
    tags: list[NewTag] | None = None,
    triggers: list[NewTrigger] | None = None,
    variables: list[NewVariable] | None = None,
    built_in_variables: list[BuiltInVariableType] | None = None,
    workspace_id: str | int | None = None,
) -> dict[str, Any]:
    """Previews adding tags, triggers, and variables to a Tag Manager workspace. Nothing changes yet.

    Nothing reaches the website until the workspace is published with
    tag_manager_preview_publish. Tags name their triggers, including new ones
    from this same preview. Items whose names are already in the workspace
    are skipped, and a tag that names a skipped trigger uses the one already
    there. workspace_id can be left out when the container has one
    workspace. Returns a change_code for apply_change.
    """
    tags, triggers, variables = tags or [], triggers or [], variables or []
    built_in_variables = built_in_variables or []
    count = len(tags) + len(triggers) + len(variables) + len(built_in_variables)
    if not count:
        raise ToolError("Give at least one tag, trigger, variable, or built-in variable.")
    if count > MAX_NEW_ITEMS:
        raise ToolError(f"Add at most {MAX_NEW_ITEMS} items at a time.")

    container = _call("GET", _container_path(account_id, container_id))
    workspace = _workspace(account_id, container_id, workspace_id)
    path = workspace["path"]
    current = _contents(path)
    details, skipped, warnings, problems = [], [], [], []

    def new_names(items, existing, label):
        taken = {_key(item.get("name", "")) for item in existing}
        chosen, seen = [], set()
        for item in items:
            name = " ".join(item.name.split())
            if not name:
                problems.append(f"A {label} has no name.")
            elif _key(name) in taken:
                skipped.append(f"{label.capitalize()} \"{name}\": already in this workspace")
            elif _key(name) in seen:
                skipped.append(f"{label.capitalize()} \"{name}\": listed twice")
            else:
                seen.add(_key(name))
                chosen.append((name, item))
        return chosen

    enabled = {b.get("type") for b in current["built_in_variables"]}
    new_built_ins = []
    for kind in built_in_variables:
        if kind in enabled:
            skipped.append(f"Built-in variable {kind}: already turned on")
        elif kind in new_built_ins:
            skipped.append(f"Built-in variable {kind}: listed twice")
        else:
            new_built_ins.append(kind)
            details.append(f"Turn on built-in variable {kind}")

    new_variables = []
    for name, variable in new_names(variables, current["variables"], "variable"):
        if not variable.type.strip():
            problems.append(f"Variable \"{name}\" has no type.")
        body = {"name": name, "type": variable.type.strip(), "parameter": _dump(variable.parameter)}
        if variable.notes:
            body["notes"] = variable.notes
        new_variables.append(body)
        details.append(f"Variable \"{name}\" (type {body['type']}){_settings_text(body['parameter'])}")

    new_triggers = []
    for name, trigger in new_names(triggers, current["triggers"], "trigger"):
        body = {"name": name, "type": trigger.type}
        for field, conditions in (
            ("filter", trigger.filter),
            ("customEventFilter", trigger.custom_event_filter),
            ("autoEventFilter", trigger.auto_event_filter),
        ):
            if conditions:
                body[field] = [c.model_dump(by_alias=True, exclude_none=True) for c in conditions]
        if trigger.parameter:
            body["parameter"] = _dump(trigger.parameter)
        for field, value in (("waitForTags", trigger.wait_for_tags), ("checkValidation", trigger.check_validation)):
            if value is not None:
                body[field] = {"type": "boolean", "value": str(value).lower()}
        if trigger.notes:
            body["notes"] = trigger.notes
        new_triggers.append(body)
        conditions = [
            _condition_text(c)
            for field in ("customEventFilter", "filter", "autoEventFilter")
            for c in body.get(field, [])
        ]
        details.append(f"Trigger \"{name}\" ({trigger.type})" + (f" when {'; '.join(conditions)}" if conditions else ""))

    existing_triggers = {t["triggerId"]: t.get("name", "") for t in current["triggers"]}
    known_ids = {**BUILT_IN_TRIGGERS, **existing_triggers}
    id_by_name = {_key(name): trigger_id for trigger_id, name in known_ids.items()}
    new_trigger_names = {_key(body["name"]): body["name"] for body in new_triggers}

    def resolve(ref: str, tag_name: str) -> dict | None:
        ref = " ".join(str(ref).split())
        if ref in known_ids:
            return {"id": ref}
        if _key(ref) in new_trigger_names:
            return {"new": new_trigger_names[_key(ref)]}
        if _key(ref) in id_by_name:
            return {"id": id_by_name[_key(ref)]}
        problems.append(f"Tag \"{tag_name}\": there's no trigger named or numbered \"{ref}\" here or in this preview.")
        return None

    def trigger_label(ref: dict) -> str:
        return f"\"{ref['new']}\" (new)" if "new" in ref else f"\"{known_ids[ref['id']]}\" ({ref['id']})"

    new_tags = []
    for name, tag in new_names(tags, current["tags"], "tag"):
        if not tag.type.strip():
            problems.append(f"Tag \"{name}\" has no type.")
        body = {"name": name, "type": tag.type.strip(), "parameter": _dump(tag.parameter)}
        if tag.paused:
            body["paused"] = True
        if tag.firing_option:
            body["tagFiringOption"] = tag.firing_option
        if tag.notes:
            body["notes"] = tag.notes
        firing = [r for r in (resolve(ref, name) for ref in dict.fromkeys(tag.firing_triggers)) if r]
        blocking = [r for r in (resolve(ref, name) for ref in dict.fromkeys(tag.blocking_triggers)) if r]
        new_tags.append({"body": body, "firing": firing, "blocking": blocking})
        text = f"Tag \"{name}\" ({body['type']})" + (", paused" if tag.paused else "")
        if firing:
            text += f", fires on {', '.join(trigger_label(r) for r in firing)}"
        if blocking:
            text += f", blocked by {', '.join(trigger_label(r) for r in blocking)}"
        details.append(text + _settings_text(body["parameter"]))
        if not tag.firing_triggers:
            warnings.append(f"Tag \"{name}\" has no firing trigger, so it won't fire.")

    if problems:
        raise ToolError("Fix these first:\n- " + "\n- ".join(problems))
    added = len(new_built_ins) + len(new_variables) + len(new_triggers) + len(new_tags)
    if not added:
        raise ToolError("Nothing to add. Everything is already in this workspace.")

    params = {
        "workspace": path,
        "workspace_url": workspace.get("tagManagerUrl"),
        "built_in_variables": new_built_ins,
        "variables": new_variables,
        "triggers": new_triggers,
        "tags": new_tags,
    }
    title = (
        f"Add {added} item(s) to workspace \"{workspace.get('name')}\" in container "
        f"\"{container.get('name')}\" ({container.get('publicId')})"
    )
    return preview_result(
        "tag_manager_new_items", "", params, title, details, skipped, warnings,
        "Checked against the workspace. Tag Manager has no test mode for adding, so it checks "
        "these for errors when publishing is previewed. Nothing has changed yet.",
    )


def _apply_new_items(payload: dict) -> dict[str, Any]:
    p = payload["params"]
    path = p["workspace"]
    new_trigger_ids = {}

    def add_trigger(body):
        new_trigger_ids[body["name"]] = _call("POST", f"{path}/triggers", body=body)["triggerId"]

    def add_tag(tag):
        body = dict(tag["body"])
        for field, refs in (("firingTriggerId", tag["firing"]), ("blockingTriggerId", tag["blocking"])):
            if refs:
                body[field] = [ref["id"] if "id" in ref else new_trigger_ids[ref["new"]] for ref in refs]
        _call("POST", f"{path}/tags", body=body)

    # Triggers before the tags that fire on them.
    steps = []
    if p["built_in_variables"]:
        steps.append((
            f"built-in variable(s) {', '.join(p['built_in_variables'])}",
            lambda: _call("POST", f"{path}/built_in_variables", params=[("type", t) for t in p["built_in_variables"]]),
        ))
    for body in p["variables"]:
        steps.append((f"variable \"{body['name']}\"", lambda body=body: _call("POST", f"{path}/variables", body=body)))
    for body in p["triggers"]:
        steps.append((f"trigger \"{body['name']}\"", lambda body=body: add_trigger(body)))
    for tag in p["tags"]:
        steps.append((f"tag \"{tag['body']['name']}\"", lambda tag=tag: add_tag(tag)))

    done = []
    for number, (label, step) in enumerate(steps):
        try:
            step()
        except ToolError as ex:
            added = f"Added before that, in the workspace and not live: {'; '.join(done)}." if done else "Nothing was added."
            not_added = "; ".join(label for label, _ in steps[number:])
            raise ToolError(
                f"Tag Manager stopped at {label}. {ex}\n{added}\nNot added: {not_added}.\n"
                "Run the preview again for the rest. Anything already added is skipped."
            ) from ex
        done.append(label)
    return {
        "done": payload["title"],
        "items_changed": len(p["built_in_variables"]) + len(p["variables"]) + len(p["triggers"]) + len(p["tags"]),
        "added": done,
        "workspace_url": p["workspace_url"],
        "note": "Nothing is live yet. To put these on the website, preview publishing with tag_manager_preview_publish.",
    }


# --- Publishing -------------------------------------------------------------


@tag_manager_mcp.tool(annotations=PREVIEW)
def preview_publish(
    account_id: str | int,
    container_id: str | int,
    version_name: str,
    version_notes: str | None = None,
    workspace_id: str | int | None = None,
) -> dict[str, Any]:
    """Previews publishing a Tag Manager workspace to the live website. Nothing changes yet.

    Lists every change in the workspace that isn't live, including changes
    other people made there, and has Tag Manager build a test version (like
    its Preview button) to check for errors. On apply_change, the workspace
    is saved as a new version named version_name and published.
    workspace_id can be left out when the container has one workspace.
    Returns a change_code for apply_change.
    """
    name = " ".join(version_name.split())
    if not name:
        raise ToolError("Give the new version a name, for example: Add Google Ads form conversion.")
    container_path = _container_path(account_id, container_id)
    container = _call("GET", container_path)
    workspace = _workspace(account_id, container_id, workspace_id)
    path, url = workspace["path"], workspace.get("tagManagerUrl")

    test = _call("POST", f"{path}:quick_preview")
    sync = test.get("syncStatus", {})
    if sync.get("mergeConflict") or sync.get("syncError"):
        raise ToolError(f"This workspace conflicts with the latest version, so it can't be published yet. Resolve it in Tag Manager first: {url}")
    if test.get("compilerError"):
        raise ToolError(f"Tag Manager found errors in this workspace, so it can't be published. Open it in Tag Manager and click Preview to see them: {url}")
    status = _call("GET", f"{path}/status")
    if status.get("mergeConflict"):
        raise ToolError(f"This workspace conflicts with the latest version, so it can't be published yet. Resolve it in Tag Manager first: {url}")
    changes = _change_entities(status)
    if not changes:
        raise ToolError("Nothing to publish. This workspace has no changes that aren't live.")

    details = [_change_text(*change) for change in changes]
    try:
        live = _call("GET", f"{container_path}/versions:live")
        details.append(f"Replaces live version {live.get('containerVersionId')} (\"{live.get('name', '')}\")")
    except ToolError:
        pass  # Only for the preview text.
    params = {
        "workspace": path,
        "workspace_url": url,
        "name": name,
        "notes": (version_notes or "").strip(),
        "status": _status_key(status),
    }
    title = (
        f"Publish workspace \"{workspace.get('name')}\" of container \"{container.get('name')}\" "
        f"({container.get('publicId')}) as version \"{name}\""
    )
    warnings = ["Publishing changes what runs on the live website as soon as it's applied."]
    return preview_result(
        "tag_manager_publish", "", params, title, details, (), warnings,
        "Passed. Tag Manager built a test version of this workspace with no errors. Nothing is published yet.",
    )


def _apply_publish(payload: dict) -> dict[str, Any]:
    p = payload["params"]
    if _status_key(_call("GET", f"{p['workspace']}/status")) != p["status"]:
        raise ToolError("The workspace changed since the preview, so nothing was published. Run the preview again.")
    created = _call("POST", f"{p['workspace']}:create_version", body={"name": p["name"], "notes": p["notes"]})
    version = created.get("containerVersion")
    sync = created.get("syncStatus", {})
    if not version or created.get("compilerError") or sync.get("mergeConflict") or sync.get("syncError"):
        raise ToolError(
            "Tag Manager couldn't save the workspace as a version, so nothing was published. "
            f"Open it in Tag Manager to see why: {p['workspace_url']}"
        )
    saved = f"Saved version {version.get('containerVersionId')} (\"{version.get('name', '')}\") but didn't publish it."
    fingerprint = version.get("fingerprint")
    try:
        published = _call("POST", f"{version['path']}:publish", params={"fingerprint": fingerprint} if fingerprint else None)
    except ToolError as ex:
        raise ToolError(f"{saved} {ex} It can be published from Versions in Tag Manager: {version.get('tagManagerUrl')}") from ex
    if published.get("compilerError"):
        raise ToolError(f"{saved} Tag Manager found errors. See them in Tag Manager: {version.get('tagManagerUrl')}")
    live = published.get("containerVersion") or version
    result = {
        "done": payload["title"],
        "version_id": live.get("containerVersionId"),
        "version_url": live.get("tagManagerUrl"),
        "note": "This is live on the website now. The earlier version stays under Versions in Tag Manager, "
        "where it can be published again to undo this.",
    }
    if created.get("newWorkspacePath"):
        result["new_workspace_id"] = created["newWorkspacePath"].rsplit("/", 1)[-1]
    return result


OTHER_APPLIERS["tag_manager_new_items"] = _apply_new_items
OTHER_APPLIERS["tag_manager_publish"] = _apply_publish

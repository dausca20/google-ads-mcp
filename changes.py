"""Tools that let Claude make a few kinds of changes in Google Ads.

Every change takes two steps:

1. A preview tool looks up what is there now, asks Google to check the change
   without making it (validate_only), and returns a change code.
2. apply_change makes the change. It only accepts a change code from a
   preview, so what gets applied is exactly what was previewed.

Change codes are signed, belong to the person who made the preview, expire
after an hour, and work once. Budget changes are left out on purpose.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf.field_mask_pb2 import FieldMask
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

import ads_mcp.utils as utils

changes_mcp = FastMCP("changes")

CHANGE_CODE_LIFETIME_SECONDS = 60 * 60
MAX_NEGATIVE_KEYWORDS = 200
MAX_STATUS_ITEMS = 100
MAX_BID_ADJUSTMENTS = 50

SMART_BIDDING = {
    "MAXIMIZE_CONVERSIONS",
    "MAXIMIZE_CONVERSION_VALUE",
    "TARGET_CPA",
    "TARGET_ROAS",
}
BID_ADJUSTABLE_TYPES = {
    "AD_SCHEDULE": "Ad schedule",
    "DEVICE": "Device",
    "LOCATION": "Location",
    "PROXIMITY": "Location radius",
}

PREVIEW = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
APPLY = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)
NEXT_STEP = (
    "Show this preview to the user. Only after they say yes, call "
    "apply_change with this change_code."
)

# Change codes that were already applied. The server runs as one instance,
# so remembering them in memory is enough to stop a code being used twice.
_used_nonces: set[str] = set()
_fallback_key = secrets.token_urlsafe(32)


# --- Change codes -----------------------------------------------------------


def _signing_key() -> bytes:
    base = os.environ.get("GOOGLE_ADS_MCP_JWT_SIGNING_KEY") or _fallback_key
    return hmac.new(b"google-ads-mcp-change-codes", base.encode(), hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest())


def _current_email() -> str:
    token = get_access_token()
    if token is None:
        return ""
    return str((token.claims or {}).get("email") or "").strip().lower()


def make_change_code(kind: str, customer_id: str, params: dict, title: str) -> str:
    payload = {
        "kind": kind,
        "customer_id": customer_id,
        "params": params,
        "title": title,
        "email": _current_email(),
        "expires": int(time.time()) + CHANGE_CODE_LIFETIME_SECONDS,
        "nonce": secrets.token_hex(8),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sign(body)}"


def read_change_code(code: str) -> dict:
    invalid = ToolError("That change code is not valid. Run the preview again.")
    try:
        body, signature = code.strip().split(".")
    except ValueError:
        raise invalid from None
    if not hmac.compare_digest(signature, _sign(body)):
        raise invalid from None
    payload = json.loads(_unb64(body))
    if payload["expires"] < time.time():
        raise ToolError("That change code expired. Run the preview again.")
    if payload["email"] != _current_email():
        raise ToolError("That change code was made by a different user.")
    if payload["nonce"] in _used_nonces:
        raise ToolError("That change was already applied.")
    return payload


# --- Talking to Google Ads --------------------------------------------------


def _google_error(ex: GoogleAdsException) -> str:
    lines = [f"Google Ads rejected the change (request ID {ex.request_id}):"]
    for error in ex.failure.errors:
        line = f"- {error.message}"
        path = ".".join(
            f"{el.field_name}[{el.index}]" if "index" in el else el.field_name
            for el in error.location.field_path_elements
        )
        if path:
            line += f" (at {path})"
        topics = [
            entry.topic
            for entry in error.details.policy_finding_details.policy_topic_entries
        ]
        if topics:
            line += f" Policy: {', '.join(topics)}."
        lines.append(line)
    return "\n".join(lines)


def _rows(customer_id: str, query: str) -> list:
    service = utils.get_googleads_service("GoogleAdsService")
    try:
        return [
            row
            for batch in service.search_stream(customer_id=customer_id, query=query)
            for row in batch.results
        ]
    except GoogleAdsException as ex:
        raise ToolError(_google_error(ex)) from ex


# Which Google Ads service and request type each kind of change goes through.
_MUTATE_CALLS = {
    "campaign_criteria": ("CampaignCriterionService", "mutate_campaign_criteria", "MutateCampaignCriteriaRequest"),
    "ad_group_criteria": ("AdGroupCriterionService", "mutate_ad_group_criteria", "MutateAdGroupCriteriaRequest"),
    "shared_criteria": ("SharedCriterionService", "mutate_shared_criteria", "MutateSharedCriteriaRequest"),
    "ad_group_ads": ("AdGroupAdService", "mutate_ad_group_ads", "MutateAdGroupAdsRequest"),
    "campaigns": ("CampaignService", "mutate_campaigns", "MutateCampaignsRequest"),
    "ad_groups": ("AdGroupService", "mutate_ad_groups", "MutateAdGroupsRequest"),
}


def _send(customer_id: str, target: str, operations: list, validate_only: bool):
    service_name, method, request_type = _MUTATE_CALLS[target]
    service = utils.get_googleads_service(service_name)
    request = utils.get_googleads_type(request_type)
    request.customer_id = customer_id
    request.operations.extend(operations)
    request.validate_only = validate_only
    try:
        return getattr(service, method)(request=request)
    except GoogleAdsException as ex:
        raise ToolError(_google_error(ex)) from ex


# --- Building the operations ------------------------------------------------
# Each builder turns the saved preview settings into Google Ads operations.
# Preview and apply both use the same builder, so they always match.


def _build_negative_keywords(client, customer_id: str, p: dict):
    operations = []
    for keyword in p["keywords"]:
        if p["level"] == "campaign":
            operation = client.get_type("CampaignCriterionOperation")
            criterion = operation.create
            criterion.campaign = f"customers/{customer_id}/campaigns/{p['target_id']}"
            criterion.negative = True
        elif p["level"] == "ad_group":
            operation = client.get_type("AdGroupCriterionOperation")
            criterion = operation.create
            criterion.ad_group = f"customers/{customer_id}/adGroups/{p['target_id']}"
            criterion.negative = True
        else:
            operation = client.get_type("SharedCriterionOperation")
            criterion = operation.create
            criterion.shared_set = f"customers/{customer_id}/sharedSets/{p['target_id']}"
        criterion.keyword.text = keyword["text"]
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[keyword["match_type"]]
        operations.append(operation)
    target = {
        "campaign": "campaign_criteria",
        "ad_group": "ad_group_criteria",
        "negative_list": "shared_criteria",
    }[p["level"]]
    return target, operations


def _build_new_search_ad(client, customer_id: str, p: dict):
    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.create
    ad_group_ad.ad_group = f"customers/{customer_id}/adGroups/{p['ad_group_id']}"
    # New ads always start paused. Turning one on is a separate change.
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.PAUSED
    ad_group_ad.ad.final_urls.append(p["final_url"])
    rsa = ad_group_ad.ad.responsive_search_ad
    for field, kind in (("headlines", "HEADLINE"), ("descriptions", "DESCRIPTION")):
        for item in p[field]:
            asset = client.get_type("AdTextAsset")
            asset.text = item["text"]
            if item.get("pin"):
                asset.pinned_field = client.enums.ServedAssetFieldTypeEnum[f"{kind}_{item['pin']}"]
            getattr(rsa, field).append(asset)
    if p.get("path1"):
        rsa.path1 = p["path1"]
    if p.get("path2"):
        rsa.path2 = p["path2"]
    return "ad_group_ads", [operation]


_STATUS_SETUP = {
    # level: (operation type, status enum, mutate target, resource path)
    "campaign": ("CampaignOperation", "CampaignStatusEnum", "campaigns", "campaigns"),
    "ad_group": ("AdGroupOperation", "AdGroupStatusEnum", "ad_groups", "adGroups"),
    "ad": ("AdGroupAdOperation", "AdGroupAdStatusEnum", "ad_group_ads", "adGroupAds"),
    "keyword": ("AdGroupCriterionOperation", "AdGroupCriterionStatusEnum", "ad_group_criteria", "adGroupCriteria"),
}


def _build_status_change(client, customer_id: str, p: dict):
    operation_type, status_enum, target, path = _STATUS_SETUP[p["level"]]
    operations = []
    for item_id in p["ids"]:
        operation = client.get_type(operation_type)
        operation.update.resource_name = f"customers/{customer_id}/{path}/{item_id}"
        operation.update.status = getattr(client.enums, status_enum)[p["status"]]
        client.copy_from(operation.update_mask, FieldMask(paths=["status"]))
        operations.append(operation)
    return target, operations


def _build_bid_adjustments(client, customer_id: str, p: dict):
    operations = []
    for change in p["changes"]:
        operation = client.get_type("CampaignCriterionOperation")
        operation.update.resource_name = (
            f"customers/{customer_id}/campaignCriteria/{p['campaign_id']}~{change['criterion_id']}"
        )
        operation.update.bid_modifier = change["bid_modifier"]
        # Named on purpose: a generated mask would drop 0.0 (-100%).
        client.copy_from(operation.update_mask, FieldMask(paths=["bid_modifier"]))
        operations.append(operation)
    return "campaign_criteria", operations


_BUILDERS = {
    "negative_keywords": _build_negative_keywords,
    "new_search_ad": _build_new_search_ad,
    "status_change": _build_status_change,
    "bid_adjustments": _build_bid_adjustments,
}


def _check_with_google(kind: str, customer_id: str, params: dict) -> None:
    client = utils.get_googleads_client()
    target, operations = _BUILDERS[kind](client, customer_id, params)
    _send(customer_id, target, operations, validate_only=True)


def _preview(kind, customer_id, params, title, details, skipped=(), warnings=()):
    _check_with_google(kind, customer_id, params)
    return {
        "preview": title,
        "changes": list(details),
        "skipped": list(skipped),
        "warnings": list(warnings),
        "google_check": "Passed. Google checked this change. Nothing has changed yet.",
        "change_code": make_change_code(kind, customer_id, params, title),
        "expires_in_minutes": CHANGE_CODE_LIFETIME_SECONDS // 60,
        "next_step": NEXT_STEP,
    }


# --- Small helpers ----------------------------------------------------------


def _id(value: str | int, name: str) -> str:
    cleaned = str(value).strip()
    if not re.fullmatch(r"\d+", cleaned):
        raise ToolError(f"{name} must be a number, got {value!r}.")
    return cleaned


def _pair_id(value: str, name: str) -> str:
    cleaned = str(value).strip()
    if not re.fullmatch(r"\d+~\d+", cleaned):
        raise ToolError(f"{name} must look like adGroupId~id (for example 123~456), got {value!r}.")
    return cleaned


MINUTES = {"ZERO": "00", "FIFTEEN": "15", "THIRTY": "30", "FORTY_FIVE": "45"}


def _percent_text(bid_modifier: float | None) -> str:
    if bid_modifier is None or bid_modifier == 1:
        return "no adjustment"
    if bid_modifier == 0:
        return "-100% (excluded)"
    return f"{round((bid_modifier - 1) * 100):+d}%"


def _schedule_text(s) -> str:
    return (
        f"{s.day_of_week.name.title()} "
        f"{s.start_hour:02d}:{MINUTES.get(s.start_minute.name, '00')}"
        f"-{s.end_hour:02d}:{MINUTES.get(s.end_minute.name, '00')}"
    )


def _pin_note(item: dict) -> str:
    return f" [pinned to position {item['pin']}]" if item["pin"] else ""


# --- Tools ------------------------------------------------------------------


class NegativeKeyword(BaseModel):
    text: str = Field(description="The keyword text, for example: free estimate")
    match_type: Literal["EXACT", "PHRASE", "BROAD"] = Field(
        description="EXACT, PHRASE, or BROAD"
    )


@changes_mcp.tool(annotations=PREVIEW)
def preview_negative_keywords(
    customer_id: str | int,
    keywords: list[NegativeKeyword],
    campaign_id: str | int | None = None,
    ad_group_id: str | int | None = None,
    negative_list_id: str | int | None = None,
) -> dict[str, Any]:
    """Previews adding negative keywords. Nothing changes yet.

    Give exactly one place to add them: campaign_id, ad_group_id, or
    negative_list_id (a shared negative keyword list, shared_set.id).
    Keywords that are already there are skipped. Returns a change_code
    for apply_change.
    """
    customer_id = utils.clean_customer_id(customer_id)
    targets = {
        "campaign": campaign_id,
        "ad_group": ad_group_id,
        "negative_list": negative_list_id,
    }
    chosen = [(level, value) for level, value in targets.items() if value not in (None, "")]
    if len(chosen) != 1:
        raise ToolError("Give exactly one of campaign_id, ad_group_id, or negative_list_id.")
    level, target_id = chosen[0]
    target_id = _id(target_id, f"{level}_id")
    if not keywords:
        raise ToolError("Give at least one keyword.")
    if len(keywords) > MAX_NEGATIVE_KEYWORDS:
        raise ToolError(f"Add at most {MAX_NEGATIVE_KEYWORDS} negative keywords at a time.")

    if level == "campaign":
        found = _rows(customer_id, f"SELECT campaign.name FROM campaign WHERE campaign.id = {target_id}")
        if not found:
            raise ToolError(f"No campaign with ID {target_id} in account {customer_id}.")
        where = f"campaign \"{found[0].campaign.name}\" ({target_id})"
        existing = _rows(
            customer_id,
            "SELECT campaign_criterion.keyword.text, campaign_criterion.keyword.match_type "
            f"FROM campaign_criterion WHERE campaign.id = {target_id} "
            "AND campaign_criterion.type = KEYWORD AND campaign_criterion.negative = TRUE",
        )
        existing_keys = {(r.campaign_criterion.keyword.text.lower(), r.campaign_criterion.keyword.match_type.name) for r in existing}
    elif level == "ad_group":
        found = _rows(
            customer_id,
            f"SELECT ad_group.name, campaign.name FROM ad_group WHERE ad_group.id = {target_id}",
        )
        if not found:
            raise ToolError(f"No ad group with ID {target_id} in account {customer_id}.")
        where = f"ad group \"{found[0].ad_group.name}\" ({target_id}) in campaign \"{found[0].campaign.name}\""
        existing = _rows(
            customer_id,
            "SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type "
            f"FROM ad_group_criterion WHERE ad_group.id = {target_id} "
            "AND ad_group_criterion.type = KEYWORD AND ad_group_criterion.negative = TRUE",
        )
        existing_keys = {(r.ad_group_criterion.keyword.text.lower(), r.ad_group_criterion.keyword.match_type.name) for r in existing}
    else:
        found = _rows(
            customer_id,
            f"SELECT shared_set.name, shared_set.type FROM shared_set WHERE shared_set.id = {target_id}",
        )
        if not found or found[0].shared_set.type_.name != "NEGATIVE_KEYWORDS":
            raise ToolError(f"No negative keyword list with ID {target_id} in account {customer_id}.")
        where = f"negative keyword list \"{found[0].shared_set.name}\" ({target_id})"
        existing = _rows(
            customer_id,
            "SELECT shared_criterion.keyword.text, shared_criterion.keyword.match_type "
            f"FROM shared_criterion WHERE shared_set.id = {target_id} "
            "AND shared_criterion.type = KEYWORD",
        )
        existing_keys = {(r.shared_criterion.keyword.text.lower(), r.shared_criterion.keyword.match_type.name) for r in existing}

    to_add, skipped, seen = [], [], set()
    for keyword in keywords:
        text = " ".join(keyword.text.split())
        if not text:
            raise ToolError("A keyword was empty.")
        key = (text.lower(), keyword.match_type)
        label = f"\"{text}\" ({keyword.match_type.lower()} match)"
        if key in existing_keys:
            skipped.append(f"{label}: already a negative there")
        elif key in seen:
            skipped.append(f"{label}: listed twice")
        else:
            seen.add(key)
            to_add.append({"text": text, "match_type": keyword.match_type})
    if not to_add:
        raise ToolError("Nothing to add. Every keyword is already a negative there.")

    params = {"level": level, "target_id": target_id, "keywords": to_add}
    title = f"Add {len(to_add)} negative keyword(s) to {where}"
    details = [f"\"{k['text']}\" ({k['match_type'].lower()} match)" for k in to_add]
    return _preview("negative_keywords", customer_id, params, title, details, skipped)


class AdText(BaseModel):
    text: str
    pin: int | None = Field(
        default=None,
        description="Optional. Pin to position 1, 2, or 3 for headlines, or 1 or 2 for descriptions.",
    )


@changes_mcp.tool(annotations=PREVIEW)
def preview_new_search_ad(
    customer_id: str | int,
    ad_group_id: str | int,
    headlines: list[AdText],
    descriptions: list[AdText],
    final_url: str,
    path1: str | None = None,
    path2: str | None = None,
) -> dict[str, Any]:
    """Previews a new responsive search ad. Nothing changes yet.

    The ad is always created PAUSED, so it can't run until someone turns it
    on with preview_status_change. Existing ads are not touched.
    Rules: 3 to 15 headlines of up to 30 characters, 2 to 4 descriptions of
    up to 90 characters, paths up to 15 characters. Returns a change_code
    for apply_change.
    """
    customer_id = utils.clean_customer_id(customer_id)
    ad_group_id = _id(ad_group_id, "ad_group_id")
    problems = []
    if not 3 <= len(headlines) <= 15:
        problems.append(f"Use 3 to 15 headlines (got {len(headlines)}).")
    if not 2 <= len(descriptions) <= 4:
        problems.append(f"Use 2 to 4 descriptions (got {len(descriptions)}).")
    for label, items, limit, pins in (
        ("Headline", headlines, 30, {1, 2, 3}),
        ("Description", descriptions, 90, {1, 2}),
    ):
        for number, item in enumerate(items, start=1):
            text = item.text.strip()
            if not text:
                problems.append(f"{label} {number} is empty.")
            elif len(text) > limit:
                problems.append(f"{label} {number} is {len(text)} characters (limit {limit}): \"{text}\"")
            if item.pin is not None and item.pin not in pins:
                problems.append(f"{label} {number} has pin {item.pin}. Use {' or '.join(map(str, sorted(pins)))}.")
    path1, path2 = (path1 or "").strip(), (path2 or "").strip()
    for label, path in (("path1", path1), ("path2", path2)):
        if len(path) > 15:
            problems.append(f"{label} is {len(path)} characters (limit 15).")
    if path2 and not path1:
        problems.append("path2 needs path1 too.")
    if not re.match(r"https?://", final_url.strip()):
        problems.append("final_url must start with https:// or http://")
    if problems:
        raise ToolError("Fix these first:\n- " + "\n- ".join(problems))

    found = _rows(
        customer_id,
        "SELECT ad_group.name, campaign.name, campaign.advertising_channel_type "
        f"FROM ad_group WHERE ad_group.id = {ad_group_id}",
    )
    if not found:
        raise ToolError(f"No ad group with ID {ad_group_id} in account {customer_id}.")
    if found[0].campaign.advertising_channel_type.name != "SEARCH":
        raise ToolError("Responsive search ads can only go in Search campaigns.")

    params = {
        "ad_group_id": ad_group_id,
        "headlines": [{"text": h.text.strip(), "pin": h.pin} for h in headlines],
        "descriptions": [{"text": d.text.strip(), "pin": d.pin} for d in descriptions],
        "final_url": final_url.strip(),
        "path1": path1,
        "path2": path2,
    }
    title = (
        f"Create a new PAUSED responsive search ad in ad group \"{found[0].ad_group.name}\" "
        f"({ad_group_id}), campaign \"{found[0].campaign.name}\""
    )
    details = (
        [f"Headline {i}: {h['text']}{_pin_note(h)}" for i, h in enumerate(params["headlines"], 1)]
        + [f"Description {i}: {d['text']}{_pin_note(d)}" for i, d in enumerate(params["descriptions"], 1)]
        + [f"Final URL: {params['final_url']}"]
        + ([f"Display path: /{params['path1']}" + (f"/{params['path2']}" if params["path2"] else "")] if params["path1"] else [])
    )
    return _preview("new_search_ad", customer_id, params, title, details)


_STATUS_LOOKUP = {
    "campaign": (
        "SELECT campaign.id, campaign.name, campaign.status FROM campaign WHERE campaign.id IN ({ids})",
        lambda r: (str(r.campaign.id), f"Campaign \"{r.campaign.name}\"", r.campaign.status.name),
    ),
    "ad_group": (
        "SELECT ad_group.id, ad_group.name, ad_group.status, campaign.name FROM ad_group WHERE ad_group.id IN ({ids})",
        lambda r: (str(r.ad_group.id), f"Ad group \"{r.ad_group.name}\" in campaign \"{r.campaign.name}\"", r.ad_group.status.name),
    ),
    "ad": (
        "SELECT ad_group.id, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status "
        "FROM ad_group_ad WHERE ad_group_ad.ad.id IN ({ids})",
        lambda r: (
            f"{r.ad_group.id}~{r.ad_group_ad.ad.id}",
            f"{r.ad_group_ad.ad.type_.name.replace('_', ' ').title()} {r.ad_group_ad.ad.id} in ad group \"{r.ad_group.name}\"",
            r.ad_group_ad.status.name,
        ),
    ),
    "keyword": (
        "SELECT ad_group.id, ad_group.name, ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, ad_group_criterion.status FROM ad_group_criterion "
        "WHERE ad_group_criterion.type = KEYWORD AND ad_group_criterion.negative = FALSE "
        "AND ad_group_criterion.criterion_id IN ({ids})",
        lambda r: (
            f"{r.ad_group.id}~{r.ad_group_criterion.criterion_id}",
            f"Keyword \"{r.ad_group_criterion.keyword.text}\" ({r.ad_group_criterion.keyword.match_type.name.lower()} match) in ad group \"{r.ad_group.name}\"",
            r.ad_group_criterion.status.name,
        ),
    ),
}


@changes_mcp.tool(annotations=PREVIEW)
def preview_status_change(
    customer_id: str | int,
    level: Literal["campaign", "ad_group", "ad", "keyword"],
    ids: list[str | int],
    status: Literal["PAUSED", "ENABLED"],
) -> dict[str, Any]:
    """Previews pausing or turning on campaigns, ad groups, ads, or keywords. Nothing changes yet.

    ids: for campaigns and ad groups, their IDs. For ads use
    adGroupId~adId, and for keywords use adGroupId~criterionId (for example
    123~456). Only one level per preview. Returns a change_code for
    apply_change.
    """
    customer_id = utils.clean_customer_id(customer_id)
    if not ids:
        raise ToolError("Give at least one ID.")
    if len(ids) > MAX_STATUS_ITEMS:
        raise ToolError(f"Change at most {MAX_STATUS_ITEMS} items at a time.")
    if level in ("campaign", "ad_group"):
        wanted = list(dict.fromkeys(_id(i, f"{level} ID") for i in ids))
        lookup_ids = wanted
    else:
        wanted = list(dict.fromkeys(_pair_id(i, f"{level} ID") for i in ids))
        lookup_ids = list(dict.fromkeys(i.split("~")[1] for i in wanted))

    query, describe = _STATUS_LOOKUP[level]
    found = {}
    for row in _rows(customer_id, query.format(ids=",".join(lookup_ids))):
        item_id, label, current = describe(row)
        found[item_id] = (label, current)

    missing = [i for i in wanted if i not in found]
    if missing:
        raise ToolError(f"Couldn't find these {level} IDs in account {customer_id}: {', '.join(missing)}")
    removed = [found[i][0] for i in wanted if found[i][1] == "REMOVED"]
    if removed:
        raise ToolError("These were removed and can't be changed: " + "; ".join(removed))

    to_change, details, skipped = [], [], []
    for item_id in wanted:
        label, current = found[item_id]
        if current == status:
            skipped.append(f"{label}: already {status.lower()}")
        else:
            to_change.append(item_id)
            details.append(f"{label}: {current.lower()} -> {status.lower()}")
    if not to_change:
        raise ToolError(f"Nothing to change. Everything is already {status.lower()}.")

    warnings = []
    if status == "ENABLED":
        warnings.append("Turning these on can start spending money right away.")
    verb = "Pause" if status == "PAUSED" else "Turn on"
    title = f"{verb} {len(to_change)} {level.replace('_', ' ')}(s)"
    params = {"level": level, "status": status, "ids": to_change}
    return _preview("status_change", customer_id, params, title, details, skipped, warnings)


class BidAdjustment(BaseModel):
    criterion_id: str | int = Field(
        description="campaign_criterion.criterion_id of a location, location radius, device, or ad schedule on this campaign"
    )
    percent: float = Field(
        description="New adjustment in percent, from -90 to 900 (for example 20 means +20%, -30 means -30%, 0 means no adjustment). Devices also allow -100 to stop showing on that device."
    )


@changes_mcp.tool(annotations=PREVIEW)
def preview_bid_adjustments(
    customer_id: str | int,
    campaign_id: str | int,
    adjustments: list[BidAdjustment],
) -> dict[str, Any]:
    """Previews changing location, device, or ad schedule bid adjustments on one campaign. Nothing changes yet.

    Only changes adjustments on locations, devices, and schedules the
    campaign already has. It never adds or removes targeting. Find
    criterion IDs by searching campaign_criterion for the campaign.
    Returns a change_code for apply_change.
    """
    customer_id = utils.clean_customer_id(customer_id)
    campaign_id = _id(campaign_id, "campaign_id")
    if not adjustments:
        raise ToolError("Give at least one adjustment.")
    if len(adjustments) > MAX_BID_ADJUSTMENTS:
        raise ToolError(f"Change at most {MAX_BID_ADJUSTMENTS} adjustments at a time.")
    wanted = {}
    for adjustment in adjustments:
        criterion_id = _id(adjustment.criterion_id, "criterion_id")
        if criterion_id in wanted:
            raise ToolError(f"criterion_id {criterion_id} is listed twice.")
        wanted[criterion_id] = adjustment.percent

    rows = _rows(
        customer_id,
        "SELECT campaign.name, campaign.bidding_strategy_type, campaign_criterion.criterion_id, "
        "campaign_criterion.type, campaign_criterion.negative, campaign_criterion.bid_modifier, "
        "campaign_criterion.device.type, campaign_criterion.location.geo_target_constant, "
        "campaign_criterion.proximity.radius, campaign_criterion.proximity.radius_units, "
        "campaign_criterion.ad_schedule.day_of_week, campaign_criterion.ad_schedule.start_hour, "
        "campaign_criterion.ad_schedule.start_minute, campaign_criterion.ad_schedule.end_hour, "
        "campaign_criterion.ad_schedule.end_minute FROM campaign_criterion "
        f"WHERE campaign.id = {campaign_id} AND campaign_criterion.criterion_id IN ({','.join(wanted)})",
    )
    found = {str(r.campaign_criterion.criterion_id): r for r in rows}
    missing = [c for c in wanted if c not in found]
    if missing:
        raise ToolError(
            f"Campaign {campaign_id} has no location, device, or schedule with these criterion IDs: {', '.join(missing)}"
        )

    location_names = {}
    geo_names = [
        r.campaign_criterion.location.geo_target_constant
        for r in rows
        if r.campaign_criterion.type_.name == "LOCATION"
    ]
    if geo_names:
        quoted = ",".join(f"'{name}'" for name in geo_names)
        try:
            for geo in _rows(
                customer_id,
                "SELECT geo_target_constant.resource_name, geo_target_constant.canonical_name "
                f"FROM geo_target_constant WHERE geo_target_constant.resource_name IN ({quoted})",
            ):
                location_names[geo.geo_target_constant.resource_name] = geo.geo_target_constant.canonical_name
        except ToolError:
            pass  # Names are only for the preview text. IDs work too.

    changes, details, problems = [], [], []
    has_non_exclusion = False
    for criterion_id, percent in wanted.items():
        criterion = found[criterion_id].campaign_criterion
        kind = criterion.type_.name
        if kind not in BID_ADJUSTABLE_TYPES:
            problems.append(f"{criterion_id} is a {kind.lower()} target. Only locations, devices, and ad schedules can be changed here.")
            continue
        if criterion.negative:
            problems.append(f"{criterion_id} is an excluded location and can't have a bid adjustment.")
            continue
        if kind == "DEVICE" and percent == -100:
            bid_modifier = 0.0
        elif -90 <= percent <= 900:
            bid_modifier = round(1 + percent / 100, 4)
            has_non_exclusion = True
        else:
            allowed = "-100, or -90 to 900" if kind == "DEVICE" else "-90 to 900"
            problems.append(f"{criterion_id}: {percent:g}% is out of range. Use {allowed}.")
            continue

        if kind == "DEVICE":
            name = criterion.device.type_.name.replace("_", " ").title()
        elif kind == "LOCATION":
            geo = criterion.location.geo_target_constant
            name = location_names.get(geo, geo)
        elif kind == "PROXIMITY":
            name = f"{criterion.proximity.radius:g} {criterion.proximity.radius_units.name.lower()} radius"
        else:
            name = _schedule_text(criterion.ad_schedule)
        current = criterion.bid_modifier if "bid_modifier" in criterion else None
        changes.append({"criterion_id": criterion_id, "bid_modifier": bid_modifier})
        details.append(
            f"{BID_ADJUSTABLE_TYPES[kind]} {name} ({criterion_id}): {_percent_text(current)} -> {_percent_text(bid_modifier)}"
        )
    if problems:
        raise ToolError("Fix these first:\n- " + "\n- ".join(problems))

    campaign = found[next(iter(wanted))].campaign
    warnings = []
    strategy = campaign.bidding_strategy_type.name
    if strategy in SMART_BIDDING and has_non_exclusion:
        warnings.append(
            f"This campaign uses Smart Bidding ({strategy.replace('_', ' ').lower()}). "
            "Google ignores bid adjustments with Smart Bidding, except a -100% device adjustment."
        )
    title = f"Change {len(changes)} bid adjustment(s) on campaign \"{campaign.name}\" ({campaign_id})"
    params = {"campaign_id": campaign_id, "changes": changes}
    return _preview("bid_adjustments", customer_id, params, title, details, warnings=warnings)


@changes_mcp.tool(annotations=APPLY)
def apply_change(change_code: str) -> dict[str, Any]:
    """Makes a change in Google Ads that a preview tool prepared.

    Only call this after showing the user the preview and getting a clear yes.
    A change code works once and expires after an hour.
    """
    payload = read_change_code(change_code)
    client = utils.get_googleads_client()
    target, operations = _BUILDERS[payload["kind"]](client, payload["customer_id"], payload["params"])
    response = _send(payload["customer_id"], target, operations, validate_only=False)
    _used_nonces.add(payload["nonce"])
    return {
        "done": payload["title"],
        "items_changed": len(response.results),
        "resource_names": [result.resource_name for result in response.results],
        "note": "This change shows in the account's Google Ads change history.",
    }

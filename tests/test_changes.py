"""Checks the change tools against the real Google Ads request types.

Google itself is replaced by a fake that records every request, so these
tests prove what would be sent, and that previews never send a real change.
"""

import time
from types import SimpleNamespace
from unittest import mock

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from google.ads.googleads.client import GoogleAdsClient
from google.oauth2.credentials import Credentials

import changes

CLIENT = GoogleAdsClient(credentials=Credentials(token="test"), use_proto_plus=True)
ENUMS = CLIENT.enums


def row(**parts):
    """Builds a real GoogleAdsRow. Values are set with dotted paths."""
    result = CLIENT.get_type("GoogleAdsRow")
    for path, value in parts.items():
        *parents, last = path.split("__")
        target = result
        for name in parents:
            target = getattr(target, name)
        setattr(target, last, value)
    return result


class FakeGoogle:
    """Stands in for Google Ads: canned query results, recorded mutations."""

    def __init__(self, results):
        self.results = results  # list of (text in query, rows)
        self.queries = []
        self.requests = []

    def rows(self, customer_id, query):
        self.queries.append(query)
        for needle, rows in self.results:
            if needle in query:
                return rows
        return []

    def service(self, name):
        google = self

        class Service:
            def __getattr__(self, method):
                def mutate(request):
                    google.requests.append((name, method, request))
                    return SimpleNamespace(
                        results=[
                            SimpleNamespace(resource_name=f"result/{i}")
                            for i, _ in enumerate(request.operations)
                        ]
                    )

                return mutate

        return Service()


def user(email="owner@example.com"):
    return AccessToken(
        token="t", client_id="c", scopes=[],
        claims={"email": email, "email_verified": "true"},
    )


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle([])
    monkeypatch.setattr(changes, "_rows", fake.rows)
    monkeypatch.setattr(changes.utils, "get_googleads_service", fake.service)
    monkeypatch.setattr(changes.utils, "get_googleads_client", lambda: CLIENT)
    monkeypatch.setattr(changes.utils, "get_googleads_type", CLIENT.get_type)
    monkeypatch.setattr(changes, "get_access_token", lambda: user())
    monkeypatch.setenv("GOOGLE_ADS_MCP_JWT_SIGNING_KEY", "test-key")
    changes._used_nonces.clear()
    return fake


# --- Negative keywords --------------------------------------------------------


def campaign_with_negatives(*existing):
    return [
        ("FROM campaign WHERE", [row(campaign__name="Cleaning - Search")]),
        (
            "FROM campaign_criterion",
            [
                row(
                    campaign_criterion__keyword__text=text,
                    campaign_criterion__keyword__match_type=ENUMS.KeywordMatchTypeEnum[match],
                )
                for text, match in existing
            ],
        ),
    ]


def test_negative_keywords_preview_only_validates(google):
    google.results = campaign_with_negatives(("jobs", "PHRASE"))
    preview = changes.preview_negative_keywords(
        customer_id="123-456-7890",
        keywords=[
            changes.NegativeKeyword(text="free", match_type="PHRASE"),
            changes.NegativeKeyword(text="  Jobs ", match_type="PHRASE"),
            changes.NegativeKeyword(text="diy  cleaning", match_type="EXACT"),
            changes.NegativeKeyword(text="free", match_type="PHRASE"),
        ],
        campaign_id="111",
    )
    assert preview["preview"] == 'Add 2 negative keyword(s) to campaign "Cleaning - Search" (111)'
    assert preview["changes"] == ['"free" (phrase match)', '"diy cleaning" (exact match)']
    assert preview["skipped"] == [
        '"Jobs" (phrase match): already a negative there',
        '"free" (phrase match): listed twice',
    ]
    [(service, method, request)] = google.requests
    assert (service, method) == ("CampaignCriterionService", "mutate_campaign_criteria")
    assert request.validate_only is True
    assert request.customer_id == "1234567890"
    created = [op.create for op in request.operations]
    assert all(c.negative for c in created)
    assert all(c.campaign == "customers/1234567890/campaigns/111" for c in created)
    assert [(c.keyword.text, c.keyword.match_type.name) for c in created] == [
        ("free", "PHRASE"),
        ("diy cleaning", "EXACT"),
    ]


def test_apply_sends_exactly_what_was_previewed(google):
    google.results = campaign_with_negatives()
    preview = changes.preview_negative_keywords(
        customer_id="1234567890",
        keywords=[changes.NegativeKeyword(text="free", match_type="BROAD")],
        campaign_id="111",
    )
    result = changes.apply_change(preview["change_code"])
    check, real = google.requests
    assert check[2].validate_only is True
    assert real[2].validate_only is False
    assert list(real[2].operations) == list(check[2].operations)
    assert result["items_changed"] == 1
    assert result["done"] == preview["preview"]


@pytest.mark.parametrize(
    "target, service, parent",
    [
        ({"ad_group_id": "222"}, "AdGroupCriterionService", "ad_group"),
        ({"negative_list_id": "333"}, "SharedCriterionService", "shared_set"),
    ],
)
def test_negative_keywords_other_levels(google, target, service, parent):
    google.results = [
        ("FROM ad_group WHERE", [row(ad_group__name="Deep Clean", campaign__name="Search")]),
        (
            "FROM shared_set WHERE",
            [row(shared_set__name="Global negatives", shared_set__type_=ENUMS.SharedSetTypeEnum.NEGATIVE_KEYWORDS)],
        ),
    ]
    changes.preview_negative_keywords(
        customer_id="1",
        keywords=[changes.NegativeKeyword(text="free", match_type="EXACT")],
        **target,
    )
    [(name, _, request)] = google.requests
    assert name == service
    created = request.operations[0].create
    assert getattr(created, parent).endswith(next(iter(target.values())))


def test_negative_keywords_needs_exactly_one_target(google):
    with pytest.raises(ToolError, match="exactly one"):
        changes.preview_negative_keywords(
            customer_id="1",
            keywords=[changes.NegativeKeyword(text="x", match_type="EXACT")],
            campaign_id="1",
            ad_group_id="2",
        )
    assert google.requests == []


def test_negative_keywords_unknown_campaign(google):
    with pytest.raises(ToolError, match="No campaign with ID 999"):
        changes.preview_negative_keywords(
            customer_id="1",
            keywords=[changes.NegativeKeyword(text="x", match_type="EXACT")],
            campaign_id="999",
        )


# --- Change codes -------------------------------------------------------------


def make_code(google):
    google.results = campaign_with_negatives()
    return changes.preview_negative_keywords(
        customer_id="1",
        keywords=[changes.NegativeKeyword(text="free", match_type="EXACT")],
        campaign_id="111",
    )["change_code"]


def test_change_code_works_once(google):
    code = make_code(google)
    changes.apply_change(code)
    with pytest.raises(ToolError, match="already applied"):
        changes.apply_change(code)


def test_tampered_change_code_is_rejected(google):
    code = make_code(google)
    body, signature = code.split(".")
    evil = changes._b64(changes._unb64(body).replace(b"111", b"999"))
    with pytest.raises(ToolError, match="not valid"):
        changes.apply_change(f"{evil}.{signature}")
    with pytest.raises(ToolError, match="not valid"):
        changes.apply_change("garbage")
    assert len(google.requests) == 1  # only the preview check


def test_expired_change_code_is_rejected(google):
    code = make_code(google)
    later = time.time() + changes.CHANGE_CODE_LIFETIME_SECONDS + 1
    with mock.patch.object(changes.time, "time", return_value=later):
        with pytest.raises(ToolError, match="expired"):
            changes.apply_change(code)


def test_change_code_from_another_user_is_rejected(google, monkeypatch):
    code = make_code(google)
    monkeypatch.setattr(changes, "get_access_token", lambda: user("helper@example.com"))
    with pytest.raises(ToolError, match="different user"):
        changes.apply_change(code)


def test_failed_apply_can_be_retried(google, monkeypatch):
    code = make_code(google)
    real_send = changes._send
    calls = []

    def fails_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ToolError("Google said no")
        return real_send(*args, **kwargs)

    monkeypatch.setattr(changes, "_send", fails_once)
    with pytest.raises(ToolError, match="Google said no"):
        changes.apply_change(code)
    assert changes.apply_change(code)["items_changed"] == 1


# --- New responsive search ads -----------------------------------------------


def search_ad_group():
    return [
        (
            "FROM ad_group WHERE",
            [
                row(
                    ad_group__name="Deep Clean",
                    campaign__name="Cleaning - Search",
                    campaign__advertising_channel_type=ENUMS.AdvertisingChannelTypeEnum.SEARCH,
                )
            ],
        )
    ]


def texts(*items):
    return [changes.AdText(text=t) if isinstance(t, str) else changes.AdText(text=t[0], pin=t[1]) for t in items]


def test_new_search_ad_is_created_paused_with_pins(google):
    google.results = search_ad_group()
    preview = changes.preview_new_search_ad(
        customer_id="1",
        ad_group_id="222",
        headlines=texts(("Get Your Weekends Back", 1), "Trusted Local Cleaners", "Book in 60 Seconds"),
        descriptions=texts("Background-checked pros. Same team every visit.", ("Free quote today.", 2)),
        final_url="https://example.com/book",
        path1="cleaning",
        path2="book",
    )
    assert "PAUSED" in preview["preview"]
    assert "Headline 1: Get Your Weekends Back [pinned to position 1]" in preview["changes"]
    assert "Display path: /cleaning/book" in preview["changes"]
    [(service, _, request)] = google.requests
    assert service == "AdGroupAdService"
    ad_group_ad = request.operations[0].create
    assert ad_group_ad.status.name == "PAUSED"
    assert ad_group_ad.ad_group == "customers/1/adGroups/222"
    rsa = ad_group_ad.ad.responsive_search_ad
    assert [h.text for h in rsa.headlines][0] == "Get Your Weekends Back"
    assert rsa.headlines[0].pinned_field.name == "HEADLINE_1"
    assert rsa.descriptions[1].pinned_field.name == "DESCRIPTION_2"
    assert "pinned_field" not in rsa.headlines[1]
    assert (rsa.path1, rsa.path2) == ("cleaning", "book")
    assert list(ad_group_ad.ad.final_urls) == ["https://example.com/book"]


def test_new_search_ad_rules_are_checked_before_google(google):
    google.results = search_ad_group()
    with pytest.raises(ToolError) as error:
        changes.preview_new_search_ad(
            customer_id="1",
            ad_group_id="222",
            headlines=texts("x" * 31, ("ok", 4)),
            descriptions=texts("fine"),
            final_url="example.com",
            path2="alone",
        )
    message = str(error.value)
    for expected in [
        "3 to 15 headlines",
        "2 to 4 descriptions",
        "Headline 1 is 31 characters",
        "Headline 2 has pin 4",
        "path2 needs path1",
        "final_url must start",
    ]:
        assert expected in message
    assert google.requests == []


def test_new_search_ad_only_in_search_campaigns(google):
    google.results = [
        ("FROM ad_group WHERE", [row(campaign__advertising_channel_type=ENUMS.AdvertisingChannelTypeEnum.DISPLAY)])
    ]
    with pytest.raises(ToolError, match="Search campaigns"):
        changes.preview_new_search_ad(
            customer_id="1", ad_group_id="222",
            headlines=texts("a", "b", "c"), descriptions=texts("d", "e"),
            final_url="https://example.com",
        )


# --- Pause or turn on ---------------------------------------------------------


def test_status_change_for_ads(google):
    google.results = [
        (
            "FROM ad_group_ad",
            [
                row(
                    ad_group__id=10, ad_group__name="Deep Clean", ad_group_ad__ad__id=77,
                    ad_group_ad__ad__type_=ENUMS.AdTypeEnum.RESPONSIVE_SEARCH_AD,
                    ad_group_ad__status=ENUMS.AdGroupAdStatusEnum.PAUSED,
                ),
                row(
                    ad_group__id=10, ad_group__name="Deep Clean", ad_group_ad__ad__id=78,
                    ad_group_ad__status=ENUMS.AdGroupAdStatusEnum.ENABLED,
                ),
            ],
        )
    ]
    preview = changes.preview_status_change(
        customer_id="1", level="ad", ids=["10~77", "10~78"], status="ENABLED"
    )
    assert preview["changes"] == ['Responsive Search Ad 77 in ad group "Deep Clean": paused -> enabled']
    assert preview["skipped"][0].endswith("already enabled")
    assert "start spending" in preview["warnings"][0]
    [(service, _, request)] = google.requests
    assert service == "AdGroupAdService"
    [operation] = request.operations
    assert operation.update.resource_name == "customers/1/adGroupAds/10~77"
    assert operation.update.status.name == "ENABLED"
    assert list(operation.update_mask.paths) == ["status"]


@pytest.mark.parametrize(
    "level, ids, needle, found, service, resource",
    [
        ("campaign", ["5"], "FROM campaign", row(campaign__id=5, campaign__status=ENUMS.CampaignStatusEnum.ENABLED),
         "CampaignService", "customers/1/campaigns/5"),
        ("ad_group", ["6"], "FROM ad_group", row(ad_group__id=6, ad_group__status=ENUMS.AdGroupStatusEnum.ENABLED),
         "AdGroupService", "customers/1/adGroups/6"),
        ("keyword", ["6~9"], "FROM ad_group_criterion",
         row(ad_group__id=6, ad_group_criterion__criterion_id=9, ad_group_criterion__status=ENUMS.AdGroupCriterionStatusEnum.ENABLED),
         "AdGroupCriterionService", "customers/1/adGroupCriteria/6~9"),
    ],
)
def test_pausing_each_level(google, level, ids, needle, found, service, resource):
    google.results = [(needle, [found])]
    preview = changes.preview_status_change(customer_id="1", level=level, ids=ids, status="PAUSED")
    assert preview["warnings"] == []
    [(name, _, request)] = google.requests
    assert name == service
    assert request.operations[0].update.resource_name == resource
    assert request.operations[0].update.status.name == "PAUSED"


def test_status_change_rejects_missing_and_removed(google):
    google.results = [
        ("FROM campaign", [row(campaign__id=5, campaign__name="Old", campaign__status=ENUMS.CampaignStatusEnum.REMOVED)])
    ]
    with pytest.raises(ToolError, match="Couldn't find these campaign IDs.*: 6"):
        changes.preview_status_change(customer_id="1", level="campaign", ids=["5", "6"], status="ENABLED")
    with pytest.raises(ToolError, match="removed"):
        changes.preview_status_change(customer_id="1", level="campaign", ids=["5"], status="ENABLED")
    with pytest.raises(ToolError, match="adGroupId~id"):
        changes.preview_status_change(customer_id="1", level="ad", ids=["77"], status="PAUSED")
    assert google.requests == []


# --- Bid adjustments ----------------------------------------------------------


def campaign_criteria(strategy="MANUAL_CPC"):
    # Google returns the campaign fields on every row.
    campaign = dict(
        campaign__name="Cleaning - Search",
        campaign__bidding_strategy_type=ENUMS.BiddingStrategyTypeEnum[strategy],
    )
    return [
        (
            "FROM campaign_criterion",
            [
                row(
                    **campaign,
                    campaign_criterion__criterion_id=30001,
                    campaign_criterion__type_=ENUMS.CriterionTypeEnum.DEVICE,
                    campaign_criterion__device__type_=ENUMS.DeviceEnum.TABLET,
                ),
                row(
                    **campaign,
                    campaign_criterion__criterion_id=1014221,
                    campaign_criterion__type_=ENUMS.CriterionTypeEnum.LOCATION,
                    campaign_criterion__location__geo_target_constant="geoTargetConstants/1014221",
                    campaign_criterion__bid_modifier=1.2,
                ),
                row(
                    **campaign,
                    campaign_criterion__criterion_id=555,
                    campaign_criterion__type_=ENUMS.CriterionTypeEnum.AD_SCHEDULE,
                    campaign_criterion__ad_schedule__day_of_week=ENUMS.DayOfWeekEnum.MONDAY,
                    campaign_criterion__ad_schedule__start_hour=8,
                    campaign_criterion__ad_schedule__start_minute=ENUMS.MinuteOfHourEnum.THIRTY,
                    campaign_criterion__ad_schedule__end_hour=17,
                    campaign_criterion__ad_schedule__end_minute=ENUMS.MinuteOfHourEnum.ZERO,
                ),
                row(
                    **campaign,
                    campaign_criterion__criterion_id=2840,
                    campaign_criterion__type_=ENUMS.CriterionTypeEnum.LOCATION,
                    campaign_criterion__negative=True,
                ),
                row(
                    **campaign,
                    campaign_criterion__criterion_id=888,
                    campaign_criterion__type_=ENUMS.CriterionTypeEnum.KEYWORD,
                ),
            ],
        ),
        (
            "FROM geo_target_constant",
            [
                row(
                    geo_target_constant__resource_name="geoTargetConstants/1014221",
                    geo_target_constant__canonical_name="Austin,Texas,United States",
                )
            ],
        ),
    ]


def adjust(*pairs):
    return [changes.BidAdjustment(criterion_id=str(c), percent=p) for c, p in pairs]


def test_bid_adjustments_preview(google):
    google.results = campaign_criteria()
    preview = changes.preview_bid_adjustments(
        customer_id="1", campaign_id="111",
        adjustments=adjust((30001, -100), (1014221, 35), (555, 0)),
    )
    assert preview["changes"] == [
        "Device Tablet (30001): no adjustment -> -100% (excluded)",
        "Location Austin,Texas,United States (1014221): +20% -> +35%",
        "Ad schedule Monday 08:30-17:00 (555): no adjustment -> no adjustment",
    ]
    assert preview["warnings"] == []
    [(service, _, request)] = google.requests
    assert service == "CampaignCriterionService"
    operations = list(request.operations)
    assert [op.update.resource_name for op in operations] == [
        "customers/1/campaignCriteria/111~30001",
        "customers/1/campaignCriteria/111~1014221",
        "customers/1/campaignCriteria/111~555",
    ]
    # Google stores bid adjustments as 32-bit floats, so 1.35 comes back slightly off.
    assert [op.update.bid_modifier for op in operations] == pytest.approx([0.0, 1.35, 1.0])
    # The -100% device change must name bid_modifier or Google would skip it.
    assert all(list(op.update_mask.paths) == ["bid_modifier"] for op in operations)


def test_bid_adjustments_warn_about_smart_bidding(google):
    google.results = campaign_criteria("TARGET_CPA")
    preview = changes.preview_bid_adjustments(customer_id="1", campaign_id="111", adjustments=adjust((1014221, 10)))
    assert "Smart Bidding" in preview["warnings"][0]
    google.requests.clear()
    preview = changes.preview_bid_adjustments(customer_id="1", campaign_id="111", adjustments=adjust((30001, -100)))
    assert preview["warnings"] == []


def test_bid_adjustments_reject_bad_input(google):
    google.results = campaign_criteria()
    with pytest.raises(ToolError) as error:
        changes.preview_bid_adjustments(
            customer_id="1", campaign_id="111",
            adjustments=adjust((1014221, -100), (555, 901), (2840, 10), (888, 10)),
        )
    message = str(error.value)
    assert "1014221: -100% is out of range. Use -90 to 900." in message
    assert "555: 901% is out of range" in message
    assert "2840 is an excluded location" in message
    assert "888 is a keyword target" in message
    with pytest.raises(ToolError, match="no location, device, or schedule with these criterion IDs: 42"):
        changes.preview_bid_adjustments(customer_id="1", campaign_id="111", adjustments=adjust((42, 10)))
    assert google.requests == []


# --- Error text ---------------------------------------------------------------


def test_google_errors_are_readable():
    failure = CLIENT.get_type("GoogleAdsFailure")
    error = CLIENT.get_type("GoogleAdsError")
    error.message = "A policy was violated."
    for name, index in (("operations", 0), ("create", None), ("headlines", 3)):
        element = type(error.location).FieldPathElement(field_name=name)
        if index is not None:
            element.index = index
        error.location.field_path_elements.append(element)
    topic = CLIENT.get_type("PolicyTopicEntry")
    topic.topic = "TRADEMARKS_IN_AD_TEXT"
    error.details.policy_finding_details.policy_topic_entries.append(topic)
    failure.errors.append(error)
    text = changes._google_error(SimpleNamespace(request_id="abc", failure=failure))
    assert "request ID abc" in text
    assert (
        "A policy was violated. (at operations[0].create.headlines[3]) "
        "Policy: TRADEMARKS_IN_AD_TEXT." in text
    )


# --- Through the MCP protocol -------------------------------------------------


def test_tools_accept_json_arguments_with_number_ids(google):
    """Calls a tool the way Claude does: JSON arguments, IDs as numbers."""
    import asyncio

    from fastmcp import Client

    import server

    google.results = campaign_with_negatives()

    async def run():
        async with Client(server.mcp) as client:
            result = await client.call_tool(
                "changes_preview_negative_keywords",
                {
                    "customer_id": 1234567890,
                    "campaign_id": 111,
                    "keywords": [{"text": "free", "match_type": "PHRASE"}],
                },
            )
            return result.structured_content

    preview = asyncio.run(run())
    assert preview["changes"] == ['"free" (phrase match)']
    assert google.requests[0][2].validate_only is True

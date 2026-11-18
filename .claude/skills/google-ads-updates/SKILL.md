---
name: google-ads-updates
description: Weekly check of the web for new Google Ads features, betas, rollouts, policy changes and deadlines, so nothing Google ships quietly gets missed. Reads Google's own Ads blog, Help Center announcements, API release notes and developer blog plus Search Engine Roundtable, PPC Land, Search Engine Land, Search Engine Journal and the Google Ads Liaison, then reports what changed, whether it is live and where it shows up. Use this skill whenever the weekly scheduled updates check fires, or whenever the user asks what's new in Google Ads, whether Google released anything, about recent Performance Max / Search / Demand Gen / Merchant Center / Local Services Ads changes, or says something like "did I miss any Google Ads updates" or "catch me up on Google Ads news", even if they don't say "weekly".
---

# Google Ads Weekly Updates

Catie runs Google Ads for clients. Google often ships features without an email: a help doc changes, a setting appears in some accounts first, or the Ads Liaison mentions it on LinkedIn. Channel budget controls for Performance Max went out that way. Your job is to catch every advertiser-facing change from the window and tell her what it is, whether she can use it yet and where it lives, so she can decide what to do with it.

When this runs from the weekly scheduled task, nobody is there to answer questions. Use the defaults, finish the report and note anything that blocked you at the end.

## Window

Default to the last 7 days ending today. If the user asks for a different range ("since October", "last two weeks"), convert it to a number of days and use that everywhere below.

## Step 1: Pull the feeds

Run the bundled script with the window in days, using this skill's base directory:

```bash
bash <skill base directory>/scripts/pull_sources.sh 7
```

It lists dated posts from the sources that can be read directly: the Google Ads & Commerce blog, Google Ads Help "New features & announcements", Google Ads API release notes, the Google Ads developer blog, Search Engine Roundtable's Google Ads category and PPC Land (filtered to Google). Search Engine Roundtable is the most useful one here because it picks up quiet help doc changes and partial rollouts. PPC Land's filter is loose, so expect consumer Gemini, Maps and YouTube creator posts mixed in.

If a section prints nothing or errors, note that source under "Didn't load" in the report and keep going.

## Step 2: Search the rest

Search Engine Land and Search Engine Journal block direct reads, and Ads Liaison posts live on LinkedIn and X, so cover them with web search. Send these searches together in one turn, in standard mode, using the current month and year:

- `Google Ads new feature [Month Year]`
- `Performance Max update [Month Year]`
- `Google Ads beta rolling out [Month Year]`
- `Google Ads Liaison Ginny Marvin [Month Year]`
- `Google Ads` with allowed_domains `searchengineland.com`
- `PPC Pulse Google Ads` with allowed_domains `searchenginejournal.com`
- `Google Ads policy update [Month Year]`
- `Google Merchant Center update [Month Year]`
- `Demand Gen YouTube ads update [Month Year]`
- `Local Services Ads update [Month Year]`

Search summaries mix in old news and sometimes show the wrong date. Only keep a search result if you can tie it to a date inside the window. When an item is headed for "The big ones", open the article (WebFetch, or `curl -sS -L -A 'Mozilla/5.0' <url>` if WebFetch is blocked) to confirm the date, the status and which campaign types it covers. Rerun a search in extended mode only when standard results come back thin.

## Step 3: Keep what matters

Keep anything that changes what an advertiser can do, see or must do in Google Ads, Merchant Center, Local Services Ads, YouTube ads or the Google tag and Tag Manager:

- new settings, controls, campaign types or ad formats
- bidding, budget and pacing changes
- new reports, metrics, insights or diagnostics
- policy changes (new rules, enforcement dates, restricted categories)
- migrations, deprecations and forced upgrades with a date
- betas, alphas and gradual rollouts, even limited ones
- Google Ads Editor and API releases only when they expose a new advertiser feature (the API often ships a feature before the UI does)

Skip organic SEO and core updates, consumer Gemini or Maps features, other ad platforms, opinion pieces, how-to guides, case studies and industry studies, unless one of them is the first report of an actual change. A help doc that only reworded something is not news. A help doc that adds a new rule or reveals a feature is.

When several sources cover the same change, merge them into one item and list every link, official source first.

## Step 4: Get the facts right

Every item needs a date and at least one link. Give each a status using only what the sources say:

- **Live**: available to all accounts
- **Rolling out**: gradual, some accounts have it
- **Beta**: open, closed or allowlist
- **Test**: alpha or experiment Google confirmed
- **Coming [date]**: announced with a future date
- **Reported only**: trade press saw it but you found no Google confirmation

If a source doesn't say which campaign types or countries a change covers, write "not stated". Never fill gaps with what seems likely. If two sources disagree, say so in the item and link both.

## Step 5: Sort

**The big ones** (up to 5): new control over spend, targeting or bidding; changes to Performance Max, Search or Local Services Ads behavior; anything that switches on by default; forced changes with a deadline; policy changes that could disapprove ads.

**Also new** (up to 12): everything else that passed Step 3. If more than 12 qualify, keep the ones closest to everyday account work and add one line saying how many smaller items were left out.

**Coming up**: dated deadlines, migrations and enforcement dates mentioned in this window's news, soonest first.

## Report

Deliver it in the chat, no document. Write it the way Catie talks: warm and plain, flowing sentences joined with "and", very few commas, no em dashes and no marketing words. Say what changed and where it is. Leave the decision about what to do for each client to her, and skip any wrap-up or recap at the end.

Use this layout:

```
**Google Ads updates: [Mon D] to [Mon D, YYYY]**

**The big ones**

**[Feature name]** · [campaign types] · [status] · [date]
[Two or three sentences: what it is, what you can now do or must do, and where it shows up in the account if a source says.]
Sources: [Google Ads Help](url) · [Search Engine Roundtable](url)

**Also new**

**[Feature name]** · [campaign types] · [status] · [date]
[One or two sentences.]
Sources: [name](url)

**Coming up**
- [Date]: [what happens] ([source](url))

**Checked:** Google Ads & Commerce blog, Google Ads Help announcements, API release notes, Ads developer blog, Search Engine Roundtable, PPC Land, Search Engine Land, Search Engine Journal, Ads Liaison posts.
**Didn't load:** [only if any]
```

A format example showing the voice (this launched in October 2026, so it is an example only and never part of a report):

> **Channel prioritization and budget allocation** · Performance Max, Demand Gen and others · Rolling out · Oct 15, 2026
> You can now mark channels as high or low priority and cap how much of the daily budget the low ones get, and a 0% cap stops spend there. Google treats the caps as best effort so a day can run a little over.
> Sources: [Search Engine Journal](https://www.searchenginejournal.com/google-ads-introduces-channel-prioritization-and-budget-allocation/592833/)

If nothing material happened, say so in one sentence and still include "Coming up" and "Checked".

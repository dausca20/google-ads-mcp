---
name: google-ads-updates
description: Weekly check of the web for big new Google Ads features, betas, rollouts and deadlines that matter for lead-gen and service business clients, so nothing Google ships quietly gets missed. Reads Google's own Ads blog, Help Center announcements, API release notes and developer blog plus Search Engine Roundtable, PPC Land, Search Engine Land, Search Engine Journal and the Google Ads Liaison, then gives a short list of the changes worth knowing. Use this skill whenever the weekly scheduled updates check fires, or whenever the user asks what's new in Google Ads, whether Google released anything, about recent Performance Max / Search / Demand Gen / Local Services Ads changes, or says something like "did I miss any Google Ads updates" or "catch me up on Google Ads news", even if they don't say "weekly".
---

# Google Ads Weekly Updates

Catie runs Google Ads for clients. Google often ships features without an email: a help doc changes, a setting appears in some accounts first, or the Ads Liaison mentions it on LinkedIn. Channel budget controls for Performance Max went out that way. Your job is to catch the big changes from the window that she could use for her clients, and keep the report short. She'll ask if she wants more detail on anything.

Her clients are lead-gen and service businesses. She doesn't run Merchant Center, Shopping or e-commerce accounts, so those changes are noise to her.

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
- `Demand Gen YouTube ads update [Month Year]`
- `Local Services Ads update [Month Year]`

Search summaries mix in old news and sometimes show the wrong date. Only keep a search result if you can tie it to a date inside the window. Before an item goes in the report, open the article (WebFetch, or `curl -sS -L -A 'Mozilla/5.0' <url>` if WebFetch is blocked) to confirm the date and status. Rerun a search in extended mode only when standard results come back thin.

## Step 3: Keep only the big ones

The bar is "a change Catie could use in a client account or needs to act on". Keep:

- new settings, controls or campaign types for Search, Performance Max, Demand Gen, YouTube or Local Services Ads
- bidding, budget and pacing changes
- new targeting, audience or exclusion options
- new reports or insights that change how you'd judge or optimize a campaign
- conversion tracking and Google tag changes that affect measurement
- defaults that switch on automatically, forced migrations and deadlines
- betas and rollouts of any of the above, even limited ones

Leave out:

- anything for Merchant Center, Shopping, product feeds, promotions, retail or e-commerce, and Hotel or Travel campaigns
- policy clarifications and rewrites, unless a new rule could get a service business's ads disapproved
- help doc touch-ups, ad label tests and small interface changes
- Google Ads Editor and API releases, unless they bring a big feature that isn't covered elsewhere (the API often ships a feature before the UI does)
- organic SEO and core updates, consumer Gemini or Maps features, other ad platforms, opinion pieces, how-to guides and studies

When several sources cover the same change, merge them into one item.

## Step 4: Get the facts right

Every item needs a date inside the window and at least one link. Work its status into the sentences using only what the sources say: live for everyone, rolling out, beta, test, coming on a date, or spotted in accounts with no word from Google yet. If a source doesn't say something, leave it out rather than guess. If two sources disagree on something that matters, say so in a few words.

## Step 5: Sort

Put the most useful changes first and stop at 6. If more than 6 qualify, add one line at the end saying how many others were left out.

**Coming up** lists dated deadlines and migrations from this window's news that affect her kind of accounts, soonest first. Leave the section out if there are none.

## Report

Deliver it in the chat, no document. Write it the way Catie talks: warm and plain, flowing sentences joined with "and", very few commas, no em dashes and no marketing words. Say what changed and leave the decision about what to do for each client to her. Skip any intro, wrap-up or recap.

Each item is a bold headline, exactly two sentences under it and a source line with one or two links, official first. Use this layout:

```
**Google Ads updates: [Mon D] to [Mon D, YYYY]**

**[Headline]**
[Two sentences: what changed and what you can now do, with its status worked in.]
[Source name](url) · [Source name](url)

**Coming up**
- [Date]: [what happens] ([source](url))

**Didn't load:** [only if any source failed]
```

A format example showing the voice (this launched in October 2026, so it is an example only and never part of a report):

> **PMax gets channel budget caps**
> You can now mark channels as high or low priority and cap how much of the daily budget the low ones get, and a 0% cap stops spend there completely. It started rolling out October 15 and Google treats the caps as best effort so a day can run a little over.
> [Search Engine Journal](https://www.searchenginejournal.com/google-ads-introduces-channel-prioritization-and-budget-allocation/592833/)

If nothing big happened, say so in one sentence and still include "Coming up" if it has anything.

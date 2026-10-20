# Google Ads and Tag Manager connector for Claude

This lets Claude look at your Google Ads accounts and Google Tag Manager containers by itself, so your skills can pull the data for you.

Claude can read everything. It can also make the changes listed under [Making changes](#making-changes), but only after showing you a preview first. It can't touch budgets.

```
Claude  ──►  your server (on Google Cloud)  ──►  Google Ads and Tag Manager
```

The Google Ads reading part is [Google's official Google Ads MCP server](https://github.com/googleads/google-ads-mcp). This repo adds four things: a lock so **only your email** can use it, the change tools, the [Tag Manager tools](#tag-manager), and a script that sets everything up for you.

## What you need

- The Google account you use to log into Google Ads.
- A credit card for Google Cloud billing. For one person's use it should fall inside Google's free tiers ([Cloud Run pricing](https://cloud.google.com/run/pricing), [Firestore pricing](https://cloud.google.com/firestore/pricing)).
- A Claude plan that allows custom connectors: Free (one only), Pro, Max, Team, or Enterprise ([Claude help](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)).

## Setup (about 30 minutes)

### 1. Make a Google Cloud project

1. Go to [console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate). Name it `google-ads-mcp` and click **Create**.
2. Go to [console.cloud.google.com/billing](https://console.cloud.google.com/billing) and link a billing account to the project.

### 2. Set up the Google sign-in screen

1. Go to [console.cloud.google.com/auth/overview](https://console.cloud.google.com/auth/overview) (make sure your new project is picked at the top) and click **Get started**.
2. App name: `Claude Google Ads`. Support email: yours.
3. Audience: **External**. Contact email: yours. Agree and click **Create**.
4. Open **Audience**, then under **Test users** click **Add users** and add the email you use for Google Ads.
5. Leave the app in **Testing** for now. On the Branding page, leave the home page, privacy policy, terms of service, and authorized domains empty.

   Later you can pick one:
   - **Stay in Testing.** Google makes you reconnect in Claude every 7 days.
   - **Publish the app.** No weekly reconnect. Google needs a home page and privacy policy link first, and your server provides both. When step 4 finishes, the script prints both links and a domain. Paste the links on the [Branding page](https://console.cloud.google.com/auth/branding), add the domain under **Authorized domains**, click **Save**, then click **Publish app** on the Audience page. The first time you sign in after that, Google shows a "Google hasn't verified this app" page. Click **Advanced**, then continue.

   Either way, the server only lets in the emails you give it in step 4.

### 3. Ask Google for Ads API access

1. Go to [the Google Ads API page](https://console.cloud.google.com/apis/library/googleads.googleapis.com) in your project and click **Enable**.
2. On the Google Ads API overview page, click **Apply for access** to get **Explorer** access. Google says most requests are approved automatically ([Google docs](https://developers.google.com/google-ads/api/docs/api-policy/access-levels)).

You don't need a developer token. Google stopped requiring them on September 9, 2026 ([Google docs](https://developers.google.com/google-ads/api/docs/concepts/no-developer-token)).

The setup script turns on the Tag Manager API for you.

### 4. Run the setup script

1. Open [Cloud Shell](https://console.cloud.google.com/?cloudshell=true). It's a terminal in your browser, so there's nothing to install.
2. Paste this and press Enter:

   ```
   git clone -b claude/google-ads-mcp-server-d4lmnp https://github.com/dausca20/google-ads-mcp.git && cd google-ads-mcp && bash setup.sh
   ```

3. Answer its questions. When it asks for a **Client ID**, follow the steps it prints. They tell you how to make your sign-in key and give you the exact link to paste.
4. When it finishes, it prints a URL that ends in `/mcp`. Copy it.

### 5. Add it to Claude

1. In Claude go to **Customize → Connectors**, click **+**, then **Add custom connector**.
2. Name: `Google Ads`. URL: the one from step 4. Click **Add**.
3. Click **Connect**, approve, pick your Google account, and click **Allow**. Google asks for Google Ads and Tag Manager. If it shows checkboxes, tick all of them.
4. Still in **Customize → Connectors**, click the **Google Ads** connector. Set **apply_change** to **Needs approval** ([Claude docs](https://claude.com/docs/connectors/custom/remote-mcp)). That's the only tool that changes your accounts, and with this setting Claude asks you every time before using it.

On a Team or Enterprise plan, an Owner adds it under **Organization settings → Connectors** first.

### 6. Try it

Ask Claude: **"What Google Ads accounts can I access?"** and **"What Tag Manager containers can I access?"**

## Making changes

Claude can do these things:

| Change | What to know |
| --- | --- |
| Add negative keywords | To a campaign, an ad group, or a shared negative keyword list. Ones that are already there get skipped. |
| Add keywords | To one ad group in a Search campaign, on or paused (you choose), with an optional max CPC. Ones already there get skipped. |
| Add callouts | To a campaign, an ad group, or the whole account, up to 25 characters each. Ones already there get skipped. |
| Create a new responsive search ad | It always starts **paused**, so nothing goes live until you turn it on. Your old ads aren't touched. |
| Pause or turn on | Campaigns, ad groups, ads, or keywords. |
| Change bid adjustments | Location, device, and ad schedule, on targets the campaign already has. It never adds or removes targeting. |
| Create conversion actions | Website conversions fired by a tag (like form submits), clicks on a phone number, and calls to a Google forwarding number on your site. Names already in the account are skipped. After they're made, Claude gives you each one's conversion ID and label to paste into Tag Manager. Google's API can't set a call action's phone number (that goes in the Tag Manager tag) or the engaged-view window. |
| Add to a Tag Manager workspace | Tags, triggers, variables, and built-in variables. A new tag can fire on a trigger added in the same change. Names already in the workspace are skipped. Nothing goes live until you publish. |
| Publish a Tag Manager workspace | Saves the workspace as a new version and publishes it to your website. The preview lists every change waiting in the workspace, including ones other people made. |

Every change happens in two steps:

1. **Preview.** Claude looks up what's there now and asks Google to test the change without making it ([Google docs](https://developers.google.com/google-ads/api/docs/concepts/api-structure)). You see a before-and-after list plus any warnings. Tag Manager has no test mode for adding items, so its error check runs in the publish preview, where Tag Manager builds a test version of the workspace first.
2. **Apply.** Only after you say yes does Claude make the change. It can only apply the exact change you previewed. A preview expires after an hour and can be used once.

The tool that makes changes is called `apply_change`. Keep it on **Needs approval** (setup step 5) so Claude asks you every time. When Claude asks, don't click **Always allow**, because that stops the asking. The preview tools never change anything.

Every change shows up in your Google Ads change history as made through the API ([Google Ads help](https://support.google.com/google-ads/answer/19888?hl=en)).

If a campaign uses Smart Bidding (like Maximize conversions or Target CPA), the preview warns you: Google ignores bid adjustments there, except a -100% device adjustment ([Google Ads help](https://support.google.com/google-ads/answer/2732132?hl=en)).

## Tag Manager

Claude can list your containers, read a workspace or the live version (tags, triggers, variables, and changes waiting to be published), add to a workspace, and publish. It works with the Tag Manager access your Google account already has, so publishing needs **Publish** permission on that container.

A typical flow: Claude creates conversion actions in Google Ads, adds the matching conversion tag and trigger to the workspace with the conversion ID and label filled in, and then previews the publish. You can test the workspace with Tag Manager's own **Preview** button before you say yes.

A publish keeps the earlier version under **Versions** in Tag Manager, and publishing that version again puts the site back the way it was.

## Good to know

- **Auction insights can't be pulled this way.** Google doesn't make them public through the API ([Google Ads API forum](https://groups.google.com/g/adwords-api/c/30s21wGZkOU)), so the auction insights part of your competitor report still needs an export you download yourself.
- **Cost:** a few uses a month should fit inside Google Cloud's free monthly allowances once your free trial ends and you upgrade to a paid account ([Cloud Run pricing](https://cloud.google.com/run/pricing)). The setup script keeps only the 2 newest copies of the server and the newest Client secret, so storage stays inside the free amounts too.
- **Staying connected:** Google Cloud shuts the server off after it sits idle (up to 15 minutes) and starts it again on your next request ([Cloud Run docs](https://cloud.google.com/run/docs/about-instance-autoscaling)). The server handles each request on its own, so Claude stays connected through that. The first request after a quiet spell can take a few extra seconds while it starts.
- **Daily limit:** Explorer access allows 2,880 operations a day ([Google docs](https://developers.google.com/google-ads/api/docs/api-policy/access-levels)).
- **Update the server or change settings** (like who can use it, or your manager account): in Cloud Shell run `cd google-ads-mcp && git pull && bash setup.sh`. Press Enter to keep anything you don't want to change.
- **Reconnect once after adding Tag Manager:** the update that adds Tag Manager needs one more permission from Google, so the connector stops working until you reconnect. In Claude go to **Customize → Connectors**, click **Google Ads**, click **Connect**, and allow everything Google asks for. If that shows an error, remove the connector and add it again (setup step 5).
- **Manager account (MCC):** if you enter one in the script, Claude reaches all your accounts through it.

## Files

| File | What it does |
| --- | --- |
| `setup.sh` | Sets up Google Cloud and starts the server |
| `server.py` | Starts Google's server and adds the email lock |
| `changes.py` | The preview and apply tools for making changes |
| `tag_manager.py` | The Tag Manager tools for reading, adding, and publishing |
| `pages.py` | The home page and privacy policy Google needs to publish the app |
| `Dockerfile` | Packs the server so Google Cloud can run it |
| `constraints.txt` | Locks the exact versions that were tested |
| `tests/` | Checks the email lock, the change tools, and the Tag Manager tools |

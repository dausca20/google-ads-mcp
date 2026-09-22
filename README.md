# Google Ads connector for Claude

This lets Claude look at your Google Ads accounts by itself, so your skills can pull the data for you.

It is **read-only**. Claude can look at your accounts, but it can't change them.

```
Claude  ──►  your server (on Google Cloud)  ──►  Google Ads
```

The server is [Google's official Google Ads MCP server](https://github.com/googleads/google-ads-mcp). This repo adds two things: a lock so **only your email** can use it, and a script that sets everything up for you.

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
5. Pick one:
   - **Leave it in Testing.** Google makes you reconnect in Claude every 7 days.
   - **Click Publish app.** No weekly reconnect. The first time you sign in, Google shows a "Google hasn't verified this app" page. Click **Advanced**, then continue.

   Either way, the server only lets in the emails you give it in step 4.

### 3. Ask Google for Ads API access

1. Go to [the Google Ads API page](https://console.cloud.google.com/apis/library/googleads.googleapis.com) in your project and click **Enable**.
2. On the Google Ads API overview page, click **Apply for access** to get **Explorer** access. Google says most requests are approved automatically ([Google docs](https://developers.google.com/google-ads/api/docs/api-policy/access-levels)).

You don't need a developer token. Google stopped requiring them on September 9, 2026 ([Google docs](https://developers.google.com/google-ads/api/docs/concepts/no-developer-token)).

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
3. Click **Connect**, approve, pick your Google account, and click **Allow**.

On a Team or Enterprise plan, an Owner adds it under **Organization settings → Connectors** first.

### 6. Try it

Ask Claude: **"What Google Ads accounts can I access?"**

## Good to know

- **Auction insights can't be pulled this way.** Google doesn't make them public through the API ([Google Ads API forum](https://groups.google.com/g/adwords-api/c/30s21wGZkOU)), so the auction insights part of your competitor report still needs an export you download yourself.
- **Daily limit:** Explorer access allows 2,880 operations a day ([Google docs](https://developers.google.com/google-ads/api/docs/api-policy/access-levels)).
- **Change settings** (like who can use it, or your manager account): in Cloud Shell run `cd google-ads-mcp && git pull && bash setup.sh`. Press Enter to keep anything you don't want to change.
- **Manager account (MCC):** if you enter one in the script, Claude reaches all your accounts through it.

## Files

| File | What it does |
| --- | --- |
| `setup.sh` | Sets up Google Cloud and starts the server |
| `server.py` | Starts Google's server and adds the email lock |
| `Dockerfile` | Packs the server so Google Cloud can run it |
| `constraints.txt` | Locks the exact versions that were tested |
| `tests/` | Checks that the email lock works |

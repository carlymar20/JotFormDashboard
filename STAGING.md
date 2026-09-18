# Setting up the staging app

A second Streamlit app, running the `staging` branch, against its own copy of
the Google Sheet. Production keeps running untouched while you test.

One-time setup, about 20 minutes.

---

## The rule that matters most

**Staging must have its own Google Sheet.**

The dashboard does not just read the sheet, it **writes** to it. Saving an
alias, editing the location list, saving quota overrides and saving operating
days all write. If both apps point at the same sheet, testing an alias on
staging silently changes production's data, and nothing warns you.

Everything else in this document is convenience. This part is the one that
will cost you real data if you skip it.

---

## Step 1 — Copy the Google Sheet

1. Open the production sheet.
2. **File → Make a copy**. Name it something unmistakable, for example
   `EPGS JotForm Dashboard — STAGING`.
3. Open the copy, click **Share**, and add your service account's
   `client_email` with **Editor** access. It is the same `client_email` that
   is in the `[gcp_service_account]` block in your Streamlit secrets.
4. Copy the new sheet's ID out of its URL. It is the long string between
   `/d/` and `/edit`.

To find the service account email without digging through secrets, open the
production app's Settings tab. Any sharing error message names it.

## Step 2 — Create the staging app

1. Go to **share.streamlit.io** and click **New app**.
2. Repository: `carlymar20/jotformdashboard`
3. Branch: **`staging`**
4. Main file path: `main.py`
5. Custom subdomain: something obvious like `epgs-jotform-staging`, so nobody
   mistakes it for production in a browser tab.
6. Deploy.

## Step 3 — Give it its own secrets

In the staging app: **Settings → Secrets**. Paste the same block as
production, with two deliberate differences.

```toml
API_KEY = "same key is fine, it only ever reads"
ADMIN_PASSWORD = "a DIFFERENT password from production"
overrides_sheet_url = "THE STAGING SHEET ID, not production's"

[gcp_service_account]
# identical to production
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "...@....iam.gserviceaccount.com"
client_id = "..."
token_uri = "https://oauth2.googleapis.com/token"
```

- `overrides_sheet_url` **must** be the staging copy. This is the whole point.
- `ADMIN_PASSWORD` should differ, so a staging password shared with someone
  testing does not also unlock production.
- `API_KEY` can be shared. The app only ever reads from JotForm, never writes.
  A second key is slightly tidier if you ever want to revoke one
  independently.

## Step 4 — Confirm they are actually separate

Before trusting it, prove the wiring:

1. Open staging, unlock admin, go to **Settings → Location list**.
2. Add a junk row, `ZZ - Staging Test`, and save.
3. Open **production** and click **Reload from Google Sheet** in the sidebar.
4. `ZZ - Staging Test` must **not** appear. If it does, staging is pointed at
   the production sheet. Stop and fix `overrides_sheet_url` before doing
   anything else.
5. Delete the junk row from staging.

---

## Day-to-day flow

```bash
git checkout staging
# make the change
git commit -am "what changed"
git push                 # staging app redeploys in ~1 minute
```

Check it on the staging URL. When it looks right:

```bash
git checkout main
git merge staging
git push                 # production redeploys
```

If production breaks, roll back by reverting the merge and pushing. Streamlit
redeploys whatever `main` currently points at, so `git revert` is a complete
rollback.

## Keeping the staging sheet honest

The staging sheet drifts from production as you test. That is fine, and it is
occasionally a problem: a bug that only shows up against real data will not
reproduce on a stale copy.

When staging data gets too far off, redo Step 1. It takes two minutes and is
better than trying to reconcile them.

---

## Things to know

**Both apps pull the same submissions.** A staging report over a wide date
range pulls the same ~90,000 Lot Audit submissions as production. Keep test
ranges to a week or two, for speed and to stay clear of JotForm's rate
limits.

**Free accounts have limits.** Streamlit Community Cloud caps how many
private apps an account can run, gives each app a fixed slice of memory, and
sleeps apps that sit idle. A sleeping staging app takes a few seconds to wake.
Check current limits in Streamlit's docs before you rely on two always-warm
apps.

**Secrets do not come from the repo.** They are per-app, set in Streamlit
Cloud. A new app starts with none, which is why Step 3 exists. Never commit
them.

**`.streamlit/config.toml` IS in the repo** and applies to both apps. It holds
the brand theme, not secrets. If you want staging to look visibly different so
nobody confuses the two, changing `primaryColor` there on the `staging` branch
only is a cheap way to do it.

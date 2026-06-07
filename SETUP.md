# Setup — Google Cloud OAuth credentials

The tool talks to Gmail through your own Google Cloud project using an OAuth
"Desktop app" client. This is a one-time setup. The read/analysis path
(`auth`, `sync`, `stats`, `reclassify`) requests only **read-only** access
(`gmail.readonly`). The cleanup path (`clean`, `undo`) requests
`gmail.modify` + `gmail.settings.basic` separately, the first time you run it,
and caches that consent in its own `token-write.json`. No permanent-delete scope
is ever requested.

## 1. Create / select a Google Cloud project

1. Go to <https://console.cloud.google.com/>.
2. Click the project picker (top bar) → **New Project**. Name it anything
   (e.g. `inbox-cleaner`) and create it. Select it once created.

## 2. Enable the Gmail API

1. Go to **APIs & Services → Library**
   (<https://console.cloud.google.com/apis/library>).
2. Search for **Gmail API**, open it, and click **Enable**.

## 3. Configure the OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**.
2. Choose **External** user type (unless you have a Workspace org), click
   **Create**.
3. Fill in the required fields (app name, your email for support + developer
   contact). You can leave the rest blank. Save and continue.
4. **Scopes**: you can skip adding scopes here; the app requests them at
   runtime. Save and continue.
5. **Test users**: add your own Gmail address as a test user. This is
   important — while the app is in "Testing" status, only listed test users can
   authorize it. Save.

> You do **not** need to publish the app or go through Google verification for
> personal use. Keeping it in "Testing" with yourself as a test user is enough.
> (Test-mode refresh tokens can expire after 7 days; if auth stops working,
> just delete `token.json` and re-run `auth`.)

## 4. Create the OAuth client credentials

1. Go to **APIs & Services → Credentials**.
2. Click **Create Credentials → OAuth client ID**.
3. Application type: **Desktop app**. Give it a name. Create.
4. In the dialog, click **Download JSON**.
5. Save that file as **`credentials.json`** in the project root (next to
   `pyproject.toml`).

`credentials.json` and the `token.json` produced after first login are both
gitignored — never commit them.

## 5. Authenticate

```bash
uv sync
uv run inbox-cleaner auth
```

The first run opens a browser asking you to grant read-only Gmail access (you
may see an "unverified app" warning — proceed since it's your own app). After
consent, the tool caches credentials to `token.json` and prints your Gmail
profile:

```
Connected to Gmail (read-only).
  Email address:  you@gmail.com
  Total messages: 48213
  Total threads:  31002
  History id:     9876543
```

Subsequent runs reuse the cached token and refresh it silently.

## Custom paths

All paths are overridable if you keep secrets elsewhere:

```bash
uv run inbox-cleaner --credentials ~/secrets/creds.json --token ~/secrets/tok.json auth
```

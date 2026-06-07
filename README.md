# Inbox Cleaner

A CLI/TUI tool to **fetch, index, group, filter, and clean** a neglected Gmail
inbox **safely**.

The design centers on one rule: **the read/analysis path is fully separated from
the write/mutation path**, and every mutation is gated behind a `plan → review →
execute` step using only reversible operations (`label`, `archive`, `trash`).

See [docs/DESIGN.md](docs/DESIGN.md) for the full design and
[SETUP.md](SETUP.md) for first-time Google Cloud / OAuth setup.

## Quick start

```bash
# 1. One-time: create OAuth credentials (see SETUP.md), save as credentials.json
# 2. Install deps and run the auth smoke test
uv sync
uv run inbox-cleaner auth
```

The `auth` command runs the OAuth consent flow once, persists a refresh token to
`token.json`, and prints your Gmail profile (email + message/thread totals) to
confirm the connection works.

## Cleaning the backlog

Once the mailbox is synced, triage and bulk-clean it without ever searching
Gmail by hand:

```bash
uv run inbox-cleaner sync          # read-only: index the mailbox locally
uv run inbox-cleaner clean         # two-pane triage TUI (writes; see below)
uv run inbox-cleaner undo          # reverse the last clean action
uv run inbox-cleaner undo --list   # show the action log
```

In `clean`, the left pane ranks sender groups (noise first) and the right pane
shows that sender's actual messages — read from the **local index**, so
inspecting a sender needs no Gmail round-trip. Set an action per sender and
execute them all in one pass:

| key | action |
|-----|--------|
| `t` | trash the sender's backlog (recoverable ~30 days) |
| `a` | archive (remove from inbox) |
| `l` | label (prompts for a name; archives + labels) |
| `k` | keep (clear the plan) |
| `f` | toggle "also create a Gmail filter" for that sender (on by default for archive/label, **off for trash**) |
| `⏎` | execute every planned action |
| `s` | incremental sync (refresh the index from Gmail, read-only) |
| `u` | undo the last action |

For each sender you act on, two things happen: the **backlog** is cleared via
`messages.batchModify`, and (if `f` is on) a **Gmail filter** is created so
future mail from that sender is auto-handled — Gmail enforces it, nothing syncs
back here.

## All commands

Every command takes the global `--credentials`, `--token`, and `--db` path
overrides (see [Custom paths](SETUP.md#custom-paths)). Read-only commands never
request a write scope.

| command | path | what it does |
|---------|------|--------------|
| `auth` | read | Run the OAuth flow and print your Gmail profile (smoke test). |
| `sync` | read | Index the mailbox locally. `--incremental` applies History-API deltas; `--query` limits ingest; `--full` re-fetches already-indexed ids. |
| `stats` | read | Local analytics over the index — where mail comes from. `--by group\|domain\|sender\|category`, `--days N`, `--noise` (rank likely-noise first), `--inbox`, `--limit`. No Gmail call. |
| `reclassify` | read | Recompute categories offline from stored headers after a classifier change. No Gmail call. |
| `clean` | **write** | Two-pane triage TUI to bulk archive/trash/label senders (the table above). `--inbox`, `--limit`. |
| `undo` | **write** | Reverse the last clean action. `--list` shows the log; `--last N` undoes the N most recent. |

A typical first run is `auth` → `sync` → `stats --noise` (to see what's worth
cleaning) → `clean`.

## Safety model

The read/analysis path (`auth`, `sync`, `stats`, `reclassify`) uses only the
**read-only** Gmail scope on `token.json`. The write path (`clean`, `undo`)
requests `gmail.modify` + `gmail.settings.basic` on a **separate**
`token-write.json`, so the analysis path can never mutate. Only reversible
operations are used (label / archive / trash — never permanent delete), and
every action is written to an `actions_log` *before* it runs so `undo` can put
it back.

## Status

All build phases from the [design doc](docs/DESIGN.md) are implemented: scaffold,
auth, store, headless sync (backfill + incremental), classification, local
`stats`, and the **executor** write path with the triage TUI and `undo`.

## Development

```bash
uv sync                 # install deps (incl. dev group)
uv run pytest -q        # run the test suite
```

The read/analysis path (auth, store, sync, classify, stats) is fully testable
without network or a terminal — the sync engine is headless and emits plain
event objects, and the formatting helpers are pure functions.

## License

[MIT](LICENSE) © Cemal Eker

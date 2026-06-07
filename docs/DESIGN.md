# Gmail Inbox Cleaner — Implementation Plan

A CLI/TUI tool to **fetch, index, group, filter, and clean** a neglected Gmail inbox **safely**. This document is the design spec; it is meant to be read by Claude Code (or a human) before any code is written.

## Working agreement

- **Confirm the design approach before generating code.** When picking up a phase, restate the plan for that phase and get a thumbs-up before writing implementation. Surface open questions first.
- Build the sync engine **headless and testable before the UI exists**. The TUI is one consumer of the engine's events, not the engine itself.
- Small, reviewable diffs per phase.

## Goal

Read-only ingest of the whole inbox into a local index, classify mail by headers, show counts grouped by category, and only then perform **reversible** cleanup actions behind an explicit review step.

## Non-negotiable safety model

The entire design is built around one rule: **separate the read/analysis path from the write/mutation path, and gate every mutation behind a plan → review → execute step that uses only reversible operations.**

- Do the entire fetch/index/classify phase with the `gmail.readonly` OAuth scope.
- Request `gmail.modify` only for the executor component. **Never** request a permanent-delete scope unless explicitly decided — `modify` can trash (recoverable ~30 days) and label/archive, but cannot permanently delete. That limitation is the guard rail.
- The rules engine **produces a plan; it never acts.** A human approves the plan.
- The executor is the only component that writes to Gmail, and it only does reversible things: `label`, `archive` (remove the `INBOX` label), `trash`. Every action is written to an `actions_log` *before* execution so it can be undone.

## Tech stack

- **Python 3.12+**
- **Textual** for the TUI — async-native, Rich-based rendering, `DataTable` widget for the grouped view, worker API for background tasks, thread-safe message passing. (Confirm exact API signatures against current Textual docs; the worker/message APIs have shifted across versions.)
- **Gmail API** via `google-api-python-client` + `google-auth-oauthlib`. The client is **blocking**, so it runs inside a Textual thread worker — it must never run on the UI event loop. (Alternative considered: `aiogoogle` for native async; deferred in favour of the more battle-tested sync client wrapped in a worker.)
- **SQLite** (stdlib `sqlite3`) + **FTS5** for the local index. The sync worker owns a thread-local connection.
- Backoff for rate limits (`tenacity` or hand-rolled exponential backoff).

## Architecture (pipeline)

```
Gmail (cloud)
   │  read-only (gmail.readonly)
   ▼
Sync (full backfill + incremental via History API)
   ▼
Local index (SQLite + FTS5)
   ▼
Enrich & classify (header parsing, sender grouping, optional clustering)
   ▼
Rules engine → PLAN (proposed actions, dry-run)
   ▼
═══ SAFETY BOUNDARY — human reviews & approves; nothing above this line mutates ═══
   ▼
Executor (reversible: label · archive · trash + audit log)  ──writes back──▶ Gmail
```

## Components

1. **Auth** — OAuth2 flow, token persistence + refresh (`google-auth-oauthlib`). Read-only scope for ingest; `modify` only for the executor.
2. **Store** — SQLite schema + migrations; thread-local connections; FTS5 virtual table.
3. **Sync engine** — backfill + incremental; emits events via an `emit` callback (knows nothing about Textual).
4. **Classifier / grouper** — derives `category` + `group_key` per message from headers.
5. **Rules engine** — declarative rules (YAML or a small Python DSL) → a plan (diff/report).
6. **Executor** — applies an approved plan via `gmail.modify`; reversible ops only; writes `actions_log`.
7. **TUI (Textual app)** — sync screen (progress + live stats) and a live grouped `DataTable`.

## Async model (the crux)

The UI thread must never block on Gmail or SQLite.

- The Textual app owns the event loop and the widgets.
- The sync engine runs as a background **thread worker** (the Gmail client is blocking).
- Engine → UI communication is via Textual messages. `post_message` is thread-safe, which is why this stays clean. The engine never touches a widget directly.
- Decouple the engine from Textual: it takes an `emit` callback and calls it with plain event objects, so it can run headless (a `--no-ui` mode) and be unit-tested.

Event/message types (illustrative shape):

```python
from dataclasses import dataclass
from textual.message import Message

@dataclass
class SyncProgress(Message):
    fetched: int
    total: int | None
    state: str            # "listing" | "fetching" | "rate_limited" | "writing"
    retry_after: float | None = None

@dataclass
class GroupSnapshot(Message):
    rows: list[tuple]     # (category, group, count, unread, oldest, newest)

@dataclass
class SyncDone(Message):
    fetched: int
    errors: int
```

Worker wiring (illustrative):

```python
class CleanerApp(App):
    def on_mount(self) -> None:
        self.run_worker(self._sync, thread=True, exclusive=True)

    def _sync(self) -> None:                      # runs off the UI thread
        engine = SyncEngine(self.store, emit=self.post_message)
        engine.backfill()                         # blocking; posts as it goes

    def on_sync_progress(self, m: SyncProgress) -> None:
        self.query_one(ProgressBar).update(total=m.total, progress=m.fetched)
        self.query_one("#status", Static).update(self._render_status(m))

    def on_group_snapshot(self, m: GroupSnapshot) -> None:
        self._refresh_table(self.query_one(DataTable), m.rows)
```

## Data model (starting point)

- `messages` — `id` (PK, Gmail message id), `thread_id`, `from_addr`, `from_domain`, `subject`, `date`, `size_estimate`, `is_unread`, `label_ids`, `snippet`, `category`, `group_key`.
- `senders` — aggregated `from_domain` / `from_addr` with counts, first/last seen.
- `sync_state` — `history_id`, last `page_token`, last full-sync timestamp.
- `actions_log` — `message_id`, `action`, `prior_state`, `timestamp`, `reversible`.
- FTS5 virtual table over `subject`, `snippet`, (lazily) `body`.
- Indexes on `category`, `group_key`, `date`.

Fetch `format=metadata` first (headers + labels + snippet — cheap); pull full bodies lazily only for messages that need search/display. Keeps the DB lean and minimises sensitive data at rest.

## Grouping / classification

Classify each message once at ingest, store `category` + `group_key`, aggregate with `GROUP BY`. Category precedence (most specific first):

```python
def classify(h: dict[str, str]) -> tuple[str, str]:
    if lid := h.get("List-Id"):
        return "mailing_list", parse_list_id(lid)        # e.g. "github.com"
    if "List-Unsubscribe" in h:
        return "newsletter", sender_domain(h)
    if h.get("Precedence", "").lower() == "bulk" or h.get("Auto-Submitted", "no") != "no":
        return "automated", sender_domain(h)
    return "personal", sender_domain(h)
```

Counts query (worker re-runs periodically, ships as `GroupSnapshot`):

```sql
SELECT category, group_key,
       COUNT(*)        AS total,
       SUM(is_unread)  AS unread,
       MIN(date)       AS oldest,
       MAX(date)       AS newest
FROM messages
GROUP BY category, group_key
ORDER BY total DESC;
```

`DataTable` columns: category, group, count, unread, oldest, newest, has-unsubscribe. UX detail: emit a snapshot every ~500 messages so the table visibly fills as the backfill streams in.

## Build phases

0. **Scaffold** — project layout, `pyproject.toml`, config object (token path, db path, scopes, batch size).
1. **Auth** — OAuth flow, token persist + refresh; smoke-test with a `getProfile` call.
2. **Store** — schema, migrations, `sync_state`, indexes.
3. **Sync engine, headless** — backfill: list IDs (paginated) → batch metadata fetch → classify → upsert, emitting events to a plain stdout consumer. Exponential backoff on `429`/`rateLimitExceeded`; persist the page token after each batch so Ctrl-C is resumable.
4. **Grouping** — classify function + aggregation query, unit-tested on header fixtures.
5. **TUI** — sync screen (progress bar, live stats, rate-limit countdown when `state == "rate_limited"`) + live grouped table. Pure wiring of worker messages to widgets.
6. **Incremental sync** — History API deltas, with the `404 → full resync` fallback (see gotchas).
7. **Polish** — graceful cancel/resume, table keybindings (sort by count, filter by category), error panel. Later: rules engine + executor (the write path — design that phase separately and carefully).

## Gotchas

- **History API retention**: Gmail keeps history for only ~a week. If the stored `history_id` is too old you get a `404` — handle that branch by falling back to a full resync.
- **Scopes**: read-only for everything except the executor; never request permanent-delete casually.
- **Idempotency**: mark processed messages so re-runs don't double-act.
- **Reversibility**: prefer `trash`/`archive`/`label` over delete; write `actions_log` before acting; a one-line `undo` walks the log backwards.
- **Validate on one bucket first**: run the executor against a single sender group before the whole backlog.
- **`dotnet build`-style trap, Python edition**: a clean import / no exception does not mean the sync logic is correct — test the engine headless against fixtures.

## Suggested first task for Claude Code

Start at Phase 0–1: scaffold the project and implement auth with token persistence, verified by a `getProfile` call printed to stdout. Confirm the approach before writing code.

"""Local SQLite index — the read/analysis side of the pipeline.

Phase 2 of the plan. This module owns persistence and nothing else: no Gmail,
no Textual. That keeps it trivially unit-testable against an in-memory or temp
database.

Design notes:

- **Thread-local connections.** The sync engine runs in a background thread and
  the TUI queries from another; sqlite connection objects are not safe to share
  across threads. :meth:`Store.connect` hands each thread its own connection,
  created lazily and configured with WAL + foreign keys.
- **Migrations via ``PRAGMA user_version``.** Dependency-free. Each schema
  version is an ordered step; :meth:`Store.connect` applies any pending steps.
- **``senders`` is a VIEW**, not a table — always consistent with ``messages``,
  no dual-write drift.
- **Upserts refresh mutable fields** (labels, read-state, category, group_key)
  so re-running a sync reflects changes made in Gmail, while staying idempotent.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 4

# Ordered migration steps. Index i (0-based) upgrades the db from user_version
# i to i+1. Append new steps; never edit a released one.
_MIGRATIONS: tuple[str, ...] = (
    # --- v0 -> v1: initial schema -------------------------------------------
    """
    CREATE TABLE messages (
        id            TEXT PRIMARY KEY,      -- Gmail message id
        thread_id     TEXT,
        from_addr     TEXT,
        from_domain   TEXT,
        subject       TEXT,
        date          INTEGER,               -- epoch ms (Gmail internalDate)
        size_estimate INTEGER,
        is_unread     INTEGER NOT NULL DEFAULT 0,  -- 0/1
        label_ids     TEXT,                  -- JSON array of label id strings
        snippet       TEXT,
        category      TEXT,
        group_key     TEXT
    );

    CREATE INDEX idx_messages_category  ON messages(category);
    CREATE INDEX idx_messages_group_key ON messages(group_key);
    CREATE INDEX idx_messages_date      ON messages(date);

    -- Aggregated senders, derived (not materialized) so it can never drift.
    CREATE VIEW senders AS
        SELECT
            from_addr,
            from_domain,
            COUNT(*)                       AS total,
            SUM(is_unread)                 AS unread,
            MIN(date)                      AS first_seen,
            MAX(date)                      AS last_seen
        FROM messages
        GROUP BY from_addr;

    -- Single-row sync cursor. The CHECK pins it to one row (id=1).
    CREATE TABLE sync_state (
        id             INTEGER PRIMARY KEY CHECK (id = 1),
        history_id     TEXT,
        page_token     TEXT,
        last_full_sync INTEGER              -- epoch ms
    );

    -- Write-path audit log. Created now; populated only by the executor in a
    -- later phase. Every mutation is logged BEFORE it happens so it can be
    -- undone.
    CREATE TABLE actions_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id  TEXT NOT NULL,
        action      TEXT NOT NULL,          -- label | archive | trash
        prior_state TEXT,                   -- JSON snapshot for undo
        timestamp   INTEGER NOT NULL,       -- epoch ms
        reversible  INTEGER NOT NULL DEFAULT 1
    );

    -- Full-text search over the cheap metadata fields. External-content table:
    -- the data lives in `messages`; this stores only the FTS index.
    CREATE VIRTUAL TABLE messages_fts USING fts5(
        subject,
        snippet,
        content='messages',
        content_rowid='rowid'
    );

    -- Triggers keep the FTS index in step with the messages table.
    CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, subject, snippet)
        VALUES (new.rowid, new.subject, new.snippet);
    END;
    CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, subject, snippet)
        VALUES ('delete', old.rowid, old.subject, old.snippet);
    END;
    CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, subject, snippet)
        VALUES ('delete', old.rowid, old.subject, old.snippet);
        INSERT INTO messages_fts(rowid, subject, snippet)
        VALUES (new.rowid, new.subject, new.snippet);
    END;
    """,
    # --- v1 -> v2: persist the headers the classifier needs, so categories can
    # be recomputed offline (reclassify) without re-fetching from Gmail. -------
    """
    ALTER TABLE messages ADD COLUMN class_headers TEXT;  -- JSON of header subset
    """,
    # --- v2 -> v3: drop the resumable page-token cursor. Backfills now always
    # start from the first page (smart-skip avoids re-fetching), so the column
    # is dead weight. ----------------------------------------------------------
    """
    ALTER TABLE sync_state DROP COLUMN page_token;
    """,
    # --- v3 -> v4: drop the FTS5 search index. It was maintained on every
    # insert/update/delete via triggers but never queried — no search command
    # exposes it — so it was pure write-time overhead. Re-add (and a search
    # command) together if full-text search is ever needed. --------------------
    """
    DROP TRIGGER IF EXISTS messages_ai;
    DROP TRIGGER IF EXISTS messages_ad;
    DROP TRIGGER IF EXISTS messages_au;
    DROP TABLE IF EXISTS messages_fts;
    """,
)

# Columns accepted by upsert_messages, in insert order.
_MESSAGE_COLUMNS: tuple[str, ...] = (
    "id",
    "thread_id",
    "from_addr",
    "from_domain",
    "subject",
    "date",
    "size_estimate",
    "is_unread",
    "label_ids",
    "snippet",
    "category",
    "group_key",
    "class_headers",
)

# Fields refreshed on conflict — the ones Gmail can change for an existing
# message. `id`, `thread_id`, `date`, `size_estimate` are immutable so are left
# out of the UPDATE clause.
_UPSERT_REFRESH_COLUMNS: tuple[str, ...] = (
    "from_addr",
    "from_domain",
    "subject",
    "is_unread",
    "label_ids",
    "snippet",
    "category",
    "group_key",
    "class_headers",
)


class Store:
    """Owns the SQLite index. One instance per database file; one connection
    per thread."""

    def __init__(self, db_path: str | Path = "inbox.sqlite") -> None:
        self.db_path = Path(db_path)
        self._local = threading.local()

    # -- connection lifecycle ------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Return this thread's connection, creating + migrating on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        # `check_same_thread=True` is fine: each thread gets its own connection.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # Wait briefly for locks instead of erroring out — matters when a
        # backfill is writing while another process (e.g. reclassify) connects.
        conn.execute("PRAGMA busy_timeout=5000")
        self._migrate(conn)
        self._local.conn = conn
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Apply any migration steps the db has not seen yet."""
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for step in _MIGRATIONS[version:]:
            conn.executescript(step)
            version += 1
            conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()

    def close(self) -> None:
        """Close this thread's connection, if any."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- messages ------------------------------------------------------------

    def upsert_messages(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Insert or refresh messages. Idempotent.

        Each row is a mapping keyed by a subset of :data:`_MESSAGE_COLUMNS`;
        missing keys default to NULL (``is_unread`` to 0). ``label_ids`` may be
        a list — it is JSON-encoded automatically.

        On an existing id, the mutable fields (labels, read-state, subject,
        category, group_key, …) are refreshed; immutable fields are left alone.

        Returns the number of rows processed.
        """
        normalized = [self._normalize_row(r) for r in rows]
        if not normalized:
            return 0

        placeholders = ", ".join(["?"] * len(_MESSAGE_COLUMNS))
        update_clause = ", ".join(
            f"{col}=excluded.{col}" for col in _UPSERT_REFRESH_COLUMNS
        )
        sql = (
            f"INSERT INTO messages ({', '.join(_MESSAGE_COLUMNS)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {update_clause}"
        )
        params = [tuple(row[col] for col in _MESSAGE_COLUMNS) for row in normalized]

        conn = self.connect()
        with conn:  # commits on success, rolls back on exception
            conn.executemany(sql, params)
        return len(params)

    @staticmethod
    def _normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
        """Fill defaults and JSON-encode label_ids for a single row."""
        if "id" not in row or row["id"] is None:
            raise ValueError("message row requires an 'id'")
        out: dict[str, Any] = {col: row.get(col) for col in _MESSAGE_COLUMNS}
        out["is_unread"] = int(bool(row.get("is_unread", 0)))
        labels = row.get("label_ids")
        if isinstance(labels, (list, tuple)):
            out["label_ids"] = json.dumps(list(labels))
        class_headers = row.get("class_headers")
        if isinstance(class_headers, dict):
            out["class_headers"] = json.dumps(class_headers)
        return out

    def delete_messages(self, ids: Iterable[str]) -> int:
        """Delete messages by id (e.g. removed/trashed in Gmail). Idempotent.

        The FTS delete trigger keeps the search index in step. Returns the
        number of ids requested.
        """
        id_list = list(ids)
        if not id_list:
            return 0
        conn = self.connect()
        removed = 0
        with conn:
            for start in range(0, len(id_list), 900):
                chunk = id_list[start : start + 900]
                placeholders = ", ".join(["?"] * len(chunk))
                cur = conn.execute(
                    f"DELETE FROM messages WHERE id IN ({placeholders})", chunk
                )
                removed += cur.rowcount
        return removed

    def existing_ids(self, ids: Iterable[str]) -> set[str]:
        """Return the subset of ``ids`` already present in the index.

        Lets the sync engine skip re-fetching mail it already has. Chunked to
        stay under SQLite's bound-parameter limit.
        """
        id_list = list(ids)
        if not id_list:
            return set()
        conn = self.connect()
        found: set[str] = set()
        for start in range(0, len(id_list), 900):
            chunk = id_list[start : start + 900]
            placeholders = ", ".join(["?"] * len(chunk))
            rows = conn.execute(
                f"SELECT id FROM messages WHERE id IN ({placeholders})", chunk
            )
            found.update(r[0] for r in rows)
        return found

    def prune_missing(self, keep_ids: Iterable[str]) -> int:
        """Delete indexed messages whose id is not in ``keep_ids``.

        Used after a full unfiltered backfill to drop rows that no longer exist
        remotely (trashed, deleted, or otherwise gone). Returns the count
        removed.
        """
        keep = set(keep_ids)
        conn = self.connect()
        local = {r[0] for r in conn.execute("SELECT id FROM messages")}
        return self.delete_messages(local - keep)

    def reclassify_all(
        self, classify_fn: "Callable[[dict[str, str]], tuple[str, str]]"
    ) -> tuple[int, int]:
        """Recompute category/group_key for every message, offline.

        Uses the persisted ``class_headers`` (no Gmail round-trip). Rows ingested
        before the v2 schema have no stored headers and cannot be reclassified
        offline.

        Returns ``(updated, skipped)`` where ``skipped`` counts rows missing
        ``class_headers`` (they need a re-sync to be reclassified).
        """
        conn = self.connect()
        skipped = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE class_headers IS NULL"
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id, class_headers FROM messages WHERE class_headers IS NOT NULL"
        ).fetchall()

        updated = 0
        with conn:
            for r in rows:
                headers = json.loads(r["class_headers"])
                category, group_key = classify_fn(headers)
                conn.execute(
                    "UPDATE messages SET category=?, group_key=? WHERE id=?",
                    (category, group_key, r["id"]),
                )
                updated += 1
        return updated, skipped

    # Dimension -> column. Whitelisted so the value can be interpolated safely.
    _STAT_DIMENSIONS = {
        "group": "group_key",
        "domain": "from_domain",
        "sender": "from_addr",
        "category": "category",
    }
    # Order mode -> ORDER BY clause (over the SELECT aliases).
    _STAT_ORDERS = {
        "recency": "newest DESC, total DESC",
        "noise": "unsub DESC, unread DESC, total DESC",
        "count": "total DESC",
    }

    def sender_stats(
        self,
        *,
        by: str = "group",
        since_ms: int | None = None,
        order: str = "recency",
        limit: int = 20,
        inbox_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Aggregated analytics over the index — where mail comes from.

        Args:
            by: grouping dimension — ``group`` (group_key), ``domain``
                (from_domain), ``sender`` (from_addr), or ``category``.
            since_ms: if set, only count messages with ``date >= since_ms``
                (epoch ms) — the recency window.
            order: ``recency`` (newest first), ``noise`` (unsubscribable +
                high-unread + high-volume first), or ``count``.
            limit: max rows.
            inbox_only: if True, count only mail still in the inbox (carrying
                the Gmail ``INBOX`` label). Note the label only drops off once
                a ``sync`` re-fetches a moved/archived message.

        Each row dict has: ``key, total, unread, oldest, newest, unsub``
        (``unsub`` is 1 if any message in the group is a newsletter/mailing
        list — i.e. realistically unsubscribable).
        """
        col = self._STAT_DIMENSIONS[by]
        order_sql = self._STAT_ORDERS[order]

        conds: list[str] = []
        params: list[Any] = []
        if since_ms is not None:
            conds.append("date >= ?")
            params.append(since_ms)
        if inbox_only:
            # label_ids is a JSON array; match the quoted token so e.g.
            # "INBOX" never matches a substring of some other label.
            conds.append("label_ids LIKE '%\"INBOX\"%'")
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        params.append(limit)

        sql = f"""
            SELECT {col} AS key,
                   COUNT(*)       AS total,
                   SUM(is_unread) AS unread,
                   MIN(date)      AS oldest,
                   MAX(date)      AS newest,
                   MAX(CASE WHEN category IN ('newsletter','mailing_list')
                            THEN 1 ELSE 0 END) AS unsub
            FROM messages
            {where}
            GROUP BY {col}
            ORDER BY {order_sql}
            LIMIT ?
        """
        conn = self.connect()
        return [dict(r) for r in conn.execute(sql, params)]

    def messages_in_group(
        self, group_key: str | None, *, inbox_only: bool = False, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return the indexed messages for one group, newest first.

        Powers the triage UI's right pane (subjects/snippets are already in the
        index, so inspecting a sender needs no Gmail round-trip). Each row dict
        has ``id, subject, snippet, date, is_unread, label_ids`` (``label_ids``
        decoded back into a list).

        Args:
            group_key: the ``group_key`` to list (``NULL`` keys match when this
                is ``None``).
            inbox_only: restrict to messages still carrying the ``INBOX`` label.
            limit: cap the number of rows (``None`` = all).
        """
        conds = ["group_key IS ?"] if group_key is None else ["group_key = ?"]
        params: list[Any] = [group_key]
        if inbox_only:
            conds.append("label_ids LIKE '%\"INBOX\"%'")
        sql = (
            "SELECT id, subject, snippet, date, is_unread, label_ids "
            "FROM messages WHERE " + " AND ".join(conds) + " ORDER BY date DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        conn = self.connect()
        out: list[dict[str, Any]] = []
        for r in conn.execute(sql, params):
            row = dict(r)
            row["label_ids"] = json.loads(row["label_ids"]) if row["label_ids"] else []
            out.append(row)
        return out

    def message_ids_in_group(
        self, group_key: str | None, *, inbox_only: bool = False
    ) -> list[str]:
        """Return just the message ids for one group — the executor's targets.

        ``inbox_only`` restricts to mail still in the inbox; useful for archive,
        where touching already-archived mail is harmless but pointless.
        """
        conds = ["group_key IS ?"] if group_key is None else ["group_key = ?"]
        params: list[Any] = [group_key]
        if inbox_only:
            conds.append("label_ids LIKE '%\"INBOX\"%'")
        sql = "SELECT id FROM messages WHERE " + " AND ".join(conds)
        conn = self.connect()
        return [r[0] for r in conn.execute(sql, params)]

    def group_sender(self, group_key: str | None) -> str | None:
        """Return the sender address shared by a group, if it has exactly one.

        Non-list groups key on the full sender address, so the group *is* the
        sender — this returns it, for building a Gmail filter's ``from:``
        criterion. Mailing-list groups span senders and return ``None`` (their
        filter matches on the list id instead).
        """
        conn = self.connect()
        rows = conn.execute(
            "SELECT DISTINCT from_addr FROM messages WHERE group_key IS ? "
            if group_key is None
            else "SELECT DISTINCT from_addr FROM messages WHERE group_key = ?",
            (group_key,),
        ).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def group_counts(self, *, inbox_only: bool = False) -> list[tuple]:
        """Return grouped counts for the live snapshot / grouped / triage view.

        Rows are ``(category, group_key, total, unread, oldest, newest)``,
        ordered by total descending. This is the aggregation the sync engine
        ships as a :class:`GroupSnapshot`; the grouped and triage views build on
        the same query. ``inbox_only`` restricts the counts to mail still
        carrying the ``INBOX`` label.
        """
        where = "WHERE label_ids LIKE '%\"INBOX\"%'" if inbox_only else ""
        conn = self.connect()
        rows = conn.execute(
            f"""
            SELECT category,
                   group_key,
                   COUNT(*)       AS total,
                   SUM(is_unread) AS unread,
                   MIN(date)      AS oldest,
                   MAX(date)      AS newest
            FROM messages
            {where}
            GROUP BY category, group_key
            ORDER BY total DESC
            """
        ).fetchall()
        return [tuple(r) for r in rows]

    # -- sync state ----------------------------------------------------------

    def get_sync_state(self) -> dict[str, Any] | None:
        """Return the sync cursor row as a dict, or None if never set."""
        conn = self.connect()
        row = conn.execute(
            "SELECT history_id, last_full_sync FROM sync_state WHERE id = 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def set_sync_state(
        self,
        *,
        history_id: str | None = None,
        last_full_sync: int | None = None,
    ) -> None:
        """Upsert the single sync-state row, overwriting the given fields."""
        conn = self.connect()
        with conn:
            conn.execute(
                """
                INSERT INTO sync_state (id, history_id, last_full_sync)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    history_id=excluded.history_id,
                    last_full_sync=excluded.last_full_sync
                """,
                (history_id, last_full_sync),
            )

    # -- actions log (write path) -------------------------------------------

    def log_action(
        self, *, target: str, action: str, prior_state: Mapping[str, Any]
    ) -> int:
        """Record an executor action *before* it runs, for undo. Returns its id.

        ``target`` is the human-facing thing acted on (a sender address or list
        id — stored in ``message_id``, which doubles as the target column for
        batch actions). ``prior_state`` is the JSON-encoded undo recipe: the
        affected ids, the inverse label delta, and any created filter id.
        """
        conn = self.connect()
        with conn:
            cur = conn.execute(
                "INSERT INTO actions_log (message_id, action, prior_state, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (target, action, json.dumps(dict(prior_state)), int(time.time() * 1000)),
            )
        return int(cur.lastrowid or 0)

    def update_action_state(self, action_id: int, prior_state: Mapping[str, Any]) -> None:
        """Overwrite a logged action's ``prior_state`` (e.g. to add a filter id
        only known after the create call returns)."""
        conn = self.connect()
        with conn:
            conn.execute(
                "UPDATE actions_log SET prior_state = ? WHERE id = ?",
                (json.dumps(dict(prior_state)), action_id),
            )

    def recent_actions(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return logged actions, newest first, each with ``prior_state`` decoded.

        ``limit`` caps the rows (``None`` = all). The shape is one dict per row:
        ``id, target, action, prior_state, timestamp``.
        """
        conn = self.connect()
        sql = (
            "SELECT id, message_id AS target, action, prior_state, timestamp "
            "FROM actions_log ORDER BY id DESC"
        )
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        out: list[dict[str, Any]] = []
        for r in conn.execute(sql, params):
            row = dict(r)
            row["prior_state"] = json.loads(row["prior_state"]) if row["prior_state"] else {}
            out.append(row)
        return out

    def delete_action(self, action_id: int) -> None:
        """Remove a logged action once it has been undone."""
        conn = self.connect()
        with conn:
            conn.execute("DELETE FROM actions_log WHERE id = ?", (action_id,))

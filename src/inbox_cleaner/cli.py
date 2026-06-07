"""Command-line entrypoint.

Wires up the subcommands: the read/analysis path (``auth``, ``sync``, ``stats``,
``reclassify``) on the read-only scope, and the write path (``clean``, ``undo``)
on the separate write token. ``clean`` launches the triage TUI; everything else
prints to stdout.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import cast

from .auth import AuthError, get_service, get_write_service
from .classify import classify
from .config import Config
from .events import GroupSnapshot, SyncDone, SyncProgress
from .executor import Executor
from .store import Store
from .sync import SyncEngine


def _cmd_auth(config: Config) -> int:
    """Authenticate and print the Gmail profile (getProfile smoke test)."""
    try:
        service = get_service(config)
    except AuthError as exc:
        print(f"Auth failed: {exc}", file=sys.stderr)
        return 1

    profile = service.users().getProfile(userId="me").execute()
    print("Connected to Gmail (read-only).")
    print(f"  Email address:  {profile.get('emailAddress')}")
    print(f"  Total messages: {profile.get('messagesTotal')}")
    print(f"  Total threads:  {profile.get('threadsTotal')}")
    print(f"  History id:     {profile.get('historyId')}")
    return 0


class _StdoutReporter:
    """Render sync events to a single, in-place status line (--no-ui).

    Everything during the run lives on one line, rewritten with a carriage
    return — progress, group count, and rate-limit notices all fold into it, so
    the sync never scrolls the terminal. Only the final summary is committed with
    a newline; error causes fold inline so it stays to one line.
    """

    def __init__(self, out: "io.TextIOBase | None" = None) -> None:
        self._out = out if out is not None else sys.stdout
        self._last_len = 0
        # Latest progress, retained so a snapshot can re-render the line without
        # losing the fetched/total counters.
        self._state = ""
        self._fetched = 0
        self._total: int | None = None
        self._retry_after: float | None = None
        self._groups: int | None = None

    def __call__(self, event: object) -> None:
        if isinstance(event, SyncProgress):
            self._state = event.state
            self._fetched = event.fetched
            self._total = event.total
            self._retry_after = event.retry_after
            self._draw(self._status_line())
        elif isinstance(event, GroupSnapshot):
            self._groups = len(event.rows)
            self._draw(self._status_line())
        elif isinstance(event, SyncDone):
            self._commit(self._summary(event))

    def _status_line(self) -> str:
        total = self._total if self._total is not None else "?"
        line = f"[{self._state:<12}] {self._fetched}/{total}"
        if self._groups is not None:
            line += f" · {self._groups} groups"
        if self._retry_after is not None:
            line += f"  (rate limited, retrying in {self._retry_after:.0f}s)"
        return line

    @staticmethod
    def _summary(event: SyncDone) -> str:
        extras = []
        if event.skipped:
            extras.append(f"{event.skipped} already indexed")
        if event.removed:
            extras.append(f"{event.removed} removed")
        if event.errors:
            causes = ", ".join(
                f"{reason}×{count}"
                for reason, count in sorted(
                    event.error_reasons.items(), key=lambda kv: kv[1], reverse=True
                )
            )
            extras.append(f"{event.errors} errors" + (f": {causes}" if causes else ""))
        suffix = f" ({'; '.join(extras)})" if extras else ""
        verb = "Cancelled" if event.cancelled else "Done"
        return f"{verb}. Fetched {event.fetched}{suffix}."

    def _draw(self, text: str) -> None:
        """Rewrite the status line in place, clearing any longer prior line."""
        pad = " " * max(self._last_len - len(text), 0)
        self._out.write("\r" + text + pad)
        self._out.flush()
        self._last_len = len(text)

    def _commit(self, text: str) -> None:
        """Overwrite the live line with the final text and end it with a newline."""
        pad = " " * max(self._last_len - len(text), 0)
        self._out.write("\r" + text + pad + "\n")
        self._out.flush()
        self._last_len = 0


def _make_stdout_emitter() -> Callable[[object], None]:
    """Return an emit callback that prints sync events to stdout (--no-ui)."""
    return _StdoutReporter()


def _cmd_sync(
    config: Config, query: str | None, incremental: bool, full: bool
) -> int:
    """Run a headless sync of the mailbox into the local index."""
    try:
        service = get_service(config)
    except AuthError as exc:
        print(f"Auth failed: {exc}", file=sys.stderr)
        return 1

    store = Store(config.db_path)
    store.connect()
    engine = SyncEngine(
        service,
        store,
        emit=_make_stdout_emitter(),
        query=query,
        batch_size=config.batch_size,
        full=full,
    )
    try:
        if incremental:
            engine.incremental()
        else:
            engine.backfill()
    except KeyboardInterrupt:
        # Messages fetched so far are already in the index; a re-run lists from
        # the start but smart-skips them, so it effectively continues from here.
        print("\nInterrupted — fetched messages saved, re-run `sync` to continue.")
        return 130
    finally:
        store.close()
    return 0


def _cmd_reclassify(config: Config) -> int:
    """Recompute categories offline from persisted headers (no Gmail call)."""
    store = Store(config.db_path)
    store.connect()
    try:
        updated, skipped = store.reclassify_all(classify)
    finally:
        store.close()
    print(f"Reclassified {updated} messages.")
    if skipped:
        print(
            f"{skipped} messages have no stored headers (ingested before this "
            "feature) — re-run `sync` to reclassify them."
        )
    return 0


def _cmd_clean(config: Config, args: argparse.Namespace) -> int:
    """Launch the triage TUI to bulk archive/trash/label senders (write path)."""
    try:
        # Acquire write credentials before the TUI takes over the terminal, so
        # the OAuth consent prompt (modify + settings scopes) happens up front.
        service = get_write_service(config)
    except AuthError as exc:
        print(f"Auth failed: {exc}", file=sys.stderr)
        return 1

    # Read-only service for the in-screen incremental sync ('s'). Optional: if it
    # can't be obtained, clean still works; sync is just disabled.
    try:
        sync_service = get_service(config)
    except AuthError as exc:
        print(f"(in-screen sync disabled: {exc})", file=sys.stderr)
        sync_service = None

    from .triage import TriageApp  # lazy: Textual import is heavy

    TriageApp(
        config,
        service=service,
        sync_service=sync_service,
        limit=args.limit,
        inbox_only=args.inbox,
    ).run()
    return 0


def _cmd_undo(config: Config, args: argparse.Namespace) -> int:
    """Reverse recent executor actions (or list them with --list)."""
    store = Store(config.db_path)
    store.connect()
    try:
        if args.list:
            rows = store.recent_actions(limit=args.last or 20)
            if not rows:
                print("No actions logged yet.")
                return 0
            print(f"Last {len(rows)} actions (newest first):\n")
            for r in rows:
                state = r["prior_state"]
                ids = len(state.get("ids", []))
                filt = " +filter" if state.get("filter_id") else ""
                when = _fmt_date(r["timestamp"])
                print(f"  #{r['id']:<4} {r['action']:<8} {ids:>5} msgs{filt:<8}  "
                      f"{when}  {r['target']}")
            return 0

        if not store.recent_actions(limit=1):
            print("Nothing to undo.")
            return 0

        try:
            service = get_write_service(config)
        except AuthError as exc:
            print(f"Auth failed: {exc}", file=sys.stderr)
            return 1

        result = Executor(service, store).undo_last(args.last)
        for r in result.results:
            status = f"error: {r.error}" if r.error else f"{r.modified} restored"
            print(f"  {r.kind:<14} {r.target:<32} {status}")
        print(f"\nUndid {len(result.results)} action(s); {result.errors} error(s).")
    finally:
        store.close()
    return 0


def _fmt_date(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _cmd_stats(config: Config, args: argparse.Namespace) -> int:
    """Local analytics over the index — where recent mail comes from."""
    since_ms = None
    if args.days and args.days > 0:
        since_ms = int((time.time() - args.days * 86400) * 1000)

    order = "noise" if args.noise else "recency"
    store = Store(config.db_path)
    store.connect()
    try:
        rows = store.sender_stats(
            by=args.by,
            since_ms=since_ms,
            order=order,
            limit=args.limit,
            inbox_only=args.inbox,
        )
    finally:
        store.close()

    window = "all time" if since_ms is None else f"last {args.days}d"
    scope = "inbox" if args.inbox else "all mail"
    mode = "noise candidates" if args.noise else "by recency"
    print(f"Top {len(rows)} {args.by}s — {scope}, {window}, {mode}:\n")

    if not rows:
        print("  (no messages indexed yet — run `sync` first)")
        return 0

    # key | count | unread | unread% | unsub | oldest | newest
    print(
        f"  {'':1} {'count':>7} {'unread':>7} {'unrd%':>6}  "
        f"{'oldest':10} {'newest':10}  {args.by}"
    )
    for r in rows:
        total = r["total"] or 0
        unread = r["unread"] or 0
        pct = f"{(unread / total * 100):.0f}%" if total else "—"
        flag = "✉" if r["unsub"] else " "
        print(
            f"  {flag:1} {total:>7} {unread:>7} {pct:>6}  "
            f"{_fmt_date(r['oldest']):10} {_fmt_date(r['newest']):10}  {r['key'] or '—'}"
        )
    print("\n  ✉ = unsubscribable (newsletter / mailing list)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inbox-cleaner",
        description="Safely fetch, index, group, and clean a Gmail inbox.",
    )
    parser.add_argument(
        "--credentials",
        help="Path to the OAuth client secret JSON (default: credentials.json).",
    )
    parser.add_argument(
        "--token",
        help="Path to the persisted token cache (default: token.json).",
    )
    parser.add_argument(
        "--db",
        help="Path to the SQLite index (default: inbox.sqlite).",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="Run the OAuth flow and print the Gmail profile.")

    sync_p = sub.add_parser(
        "sync", help="Sync the mailbox into the local index (headless)."
    )
    sync_p.add_argument(
        "--query",
        default=None,
        help="Gmail search query to limit ingest (default: whole mailbox). "
        "e.g. 'in:inbox' or 'older_than:1y'.",
    )
    sync_p.add_argument(
        "--incremental",
        action="store_true",
        help="Apply History API deltas since the last sync (falls back to a "
        "full backfill if never synced or the history has expired).",
    )
    sync_p.add_argument(
        "--full",
        action="store_true",
        help="Re-fetch every message even if already indexed (default skips "
        "ids already in the local DB).",
    )

    sub.add_parser(
        "reclassify",
        help="Recompute categories offline from stored headers (no Gmail call).",
    )

    stats_p = sub.add_parser(
        "stats", help="Local analytics on the indexed mail (no Gmail call)."
    )
    stats_p.add_argument(
        "--days",
        type=int,
        default=0,
        help="Only count mail from the last N days (0 = all time, default).",
    )
    stats_p.add_argument(
        "--by",
        choices=["group", "domain", "sender", "category"],
        default="group",
        help="Grouping dimension (default: group).",
    )
    stats_p.add_argument(
        "--limit", type=int, default=20, help="Number of rows to show."
    )
    stats_p.add_argument(
        "--noise",
        action="store_true",
        help="Rank likely-noise senders first (unsubscribable, high unread).",
    )
    stats_p.add_argument(
        "--inbox",
        action="store_true",
        help="Count only mail still in the inbox (run `sync` to reflect moves).",
    )

    clean_p = sub.add_parser(
        "clean",
        help="Triage senders in a TUI and bulk archive/trash/label them (writes).",
    )
    clean_p.add_argument(
        "--limit", type=int, default=200, help="Max sender groups to load (default 200)."
    )
    clean_p.add_argument(
        "--inbox",
        action="store_true",
        help="Only act on mail still in the inbox.",
    )

    undo_p = sub.add_parser(
        "undo", help="Reverse recent clean actions (or list them with --list)."
    )
    undo_p.add_argument(
        "--last", type=int, default=1, help="How many recent actions to undo (default 1)."
    )
    undo_p.add_argument(
        "--list", action="store_true", help="List logged actions instead of undoing."
    )

    return parser


def _config_from_args(args: argparse.Namespace) -> Config:
    overrides = {}
    if args.credentials:
        overrides["credentials_path"] = args.credentials
    if args.token:
        overrides["token_path"] = args.token
    if args.db:
        overrides["db_path"] = args.db
    return Config(**overrides)


def main(argv: list[str] | None = None) -> int:
    # Ensure non-ASCII output (em dash, ✉, …) doesn't crash on legacy consoles
    # (e.g. Windows cp1252). No-op where stdout can't be reconfigured.
    try:
        cast(io.TextIOWrapper, sys.stdout).reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    args = build_parser().parse_args(argv)
    config = _config_from_args(args)

    if args.command == "auth":
        return _cmd_auth(config)
    if args.command == "sync":
        return _cmd_sync(config, args.query, args.incremental, args.full)
    if args.command == "reclassify":
        return _cmd_reclassify(config)
    if args.command == "stats":
        return _cmd_stats(config, args)
    if args.command == "clean":
        return _cmd_clean(config, args)
    if args.command == "undo":
        return _cmd_undo(config, args)

    # argparse with required=True prevents reaching here.
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

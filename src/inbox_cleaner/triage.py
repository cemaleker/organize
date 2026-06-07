"""Triage TUI — the write path's front end (Phase 7).

A two-pane Textual app for clearing a backlog fast, without ever searching
Gmail by hand:

- **Left pane:** sender groups ranked noise-first (the same ``group_counts``
  aggregation the ``stats`` command uses), each carrying a *planned* action you
  set with a keypress.
- **Right pane:** the highlighted sender's actual messages, read straight from
  the local index — subjects/snippets are already stored, so inspecting a sender
  costs no Gmail round-trip.

Keys: ``t`` trash · ``a`` archive · ``l`` label · ``k`` keep · ``f`` toggle
"also create a filter" · ``⏎`` execute every planned action · ``u`` undo last.

Execution and undo run in thread workers (the Gmail client is blocking) through
the :class:`~inbox_cleaner.executor.Executor`; the UI thread only renders. The
pure display helpers (:func:`triage_view`, :func:`sender_cells`,
:func:`message_cells`) are split out so they can be unit-tested without a
terminal.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static

from .config import Config
from .events import STATE_RATE_LIMITED, SyncProgress
from .executor import Action, ApplyResult, Executor
from .store import Store
from .sync import SyncEngine

# Per-action display abbreviation for the left pane's "action" column.
_ACTION_ABBR = {"trash": "TRASH", "archive": "ARCHIVE", "label": "LABEL", "keep": ""}

_SENDER_COLUMNS = ("action", "sender / list", "count", "unread", "category", "flt")
_MESSAGE_COLUMNS = ("date", "", "subject")

# Categories that are inherently unsubscribe-able — drives the noise ranking.
_UNSUB_CATEGORIES = frozenset({"newsletter", "mailing_list"})


def _noise_key(row: tuple) -> tuple[int, int, int]:
    """Rank likely-noise groups first: unsubscribable, then unread, then volume.

    Mirrors the CLI's ``--noise`` order (``unsub DESC, unread DESC, total DESC``);
    ``unsub`` is derived from the category. Rows are
    ``(category, group_key, total, unread, oldest, newest)``.
    """
    unsub = 1 if row[0] in _UNSUB_CATEGORIES else 0
    return (unsub, row[3] or 0, row[2] or 0)


def _fmt_date(ms: int | None) -> str:
    """Format an epoch-ms timestamp as a UTC date, or an em dash if missing."""
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def format_status(event: SyncProgress) -> str:
    """Render a one-line status from a sync progress event (for the 's' action)."""
    total = event.total if event.total is not None else "?"
    if event.state == STATE_RATE_LIMITED and event.retry_after is not None:
        return (
            f"⏳ rate limited — retrying in {event.retry_after:.0f}s "
            f"({event.fetched}/{total})"
        )
    return f"{event.state} … {event.fetched}/{total}"


@dataclass
class _Planned:
    """A pending triage decision for one group, before it is executed."""

    kind: str                      # trash | archive | label | keep
    label_name: str | None = None
    create_filter: bool = True


def triage_view(rows: list[tuple], limit: int | None = None) -> list[tuple]:
    """Rank group rows for triage (noise first) and cap to ``limit`` — pure.

    Rows are ``(category, group_key, total, unread, oldest, newest)``, the shape
    :meth:`Store.group_counts` returns.
    """
    view = sorted(rows, key=_noise_key, reverse=True)
    return view[:limit] if limit else view


def plan_label(planned: "_Planned | None") -> str:
    """Render a planned action for the left pane's action column."""
    if planned is None:
        return ""
    if planned.kind == "label" and planned.label_name:
        return f"LABEL:{planned.label_name}"
    return _ACTION_ABBR.get(planned.kind, "")


def sender_cells(row: tuple, planned: "_Planned | None") -> tuple[str, ...]:
    """Build the left-pane display cells for one group + its planned action."""
    category, group_key, total, unread, _oldest, _newest = row
    flt = "✓" if (planned and planned.kind != "keep" and planned.create_filter) else ""
    return (
        plan_label(planned),
        group_key or "—",
        str(total),
        str(unread or 0),
        category or "—",
        flt,
    )


def message_cells(msg: dict) -> tuple[str, ...]:
    """Build the right-pane display cells for one indexed message."""
    return (_fmt_date(msg["date"]), "✉" if msg["is_unread"] else "", msg["subject"] or "—")


class _LabelPrompt(ModalScreen[str]):
    """A tiny modal asking for a label name (for the 'l' action)."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="label_dialog"):
            yield Static("Apply which label? (archives + labels matching mail)")
            yield Input(placeholder="e.g. Promotions", id="label_input")

    def on_mount(self) -> None:
        self.query_one("#label_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss("")


class TriageApp(App):
    """Two-pane triage UI: decide an action per sender, execute in bulk."""

    CSS = """
    #panes    { height: 1fr; }
    #senders  { width: 55%; }
    #messages { width: 45%; border-left: solid $primary; }
    #status   { height: 1; margin: 0 1; color: $text-muted; }
    #status.error { color: $error; text-style: bold; }
    #label_dialog {
        width: 60; height: auto; padding: 1 2; border: thick $primary;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("t", "plan('trash')", "Trash"),
        Binding("a", "plan('archive')", "Archive"),
        Binding("l", "plan_label", "Label"),
        Binding("k", "plan('keep')", "Keep"),
        Binding("f", "toggle_filter", "±Filter"),
        Binding("enter", "execute", "Execute", priority=True),
        Binding("s", "sync", "Sync"),
        Binding("u", "undo", "Undo last"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config: Config,
        *,
        service: object | None = None,
        sync_service: object | None = None,
        limit: int = 200,
        inbox_only: bool = False,
    ) -> None:
        super().__init__()
        self.config = config
        self.service = service
        # Read-only service used for the incremental sync action. Kept separate
        # from the write `service` so the refresh path uses the read-only scope,
        # per the safety model.
        self.sync_service = sync_service
        self.limit = limit
        self.inbox_only = inbox_only
        self.store = Store(config.db_path)
        self.last_status = ""  # mirror of the status line, for testing

        self._rows: list[tuple] = []            # ranked triage rows (display order)
        self._plan: dict[str, _Planned] = {}    # group_key -> pending decision
        self._syncing = False
        # Lets a long sync be interrupted on quit (the blocking Gmail client runs
        # in a thread that can't be force-killed).
        self._cancel_event = threading.Event()

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panes"):
            yield DataTable(id="senders")
            yield DataTable(id="messages")
        yield Static("loading…", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "inbox-cleaner — triage"
        senders = self.query_one("#senders", DataTable)
        senders.add_columns(*_SENDER_COLUMNS)
        senders.cursor_type = "row"
        senders.zebra_stripes = True
        messages = self.query_one("#messages", DataTable)
        messages.add_columns(*_MESSAGE_COLUMNS)
        messages.cursor_type = "row"
        self._update_subtitle()
        self.run_worker(self._load, thread=True, exclusive=True)

    def on_unmount(self) -> None:
        # Ask an in-progress sync to stop so the process doesn't hang on exit.
        self._cancel_event.set()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Suppress the priority 'execute' binding while a modal is open.

        Enter is a *priority* binding so ⏎ executes even when the senders table
        has focus. Textual checks priority bindings before the focused widget,
        so without this the label prompt's Enter would run a bulk execute
        instead of submitting the input. Disabling 'execute' while a screen is
        pushed lets Enter fall through to the modal's Input (which then submits).
        """
        if action == "execute" and len(self.screen_stack) > 1:
            return False
        return True

    # -- workers (off the UI thread) -----------------------------------------

    def _load(self) -> None:
        rows = self.store.group_counts(inbox_only=self.inbox_only)
        self.call_from_thread(self._set_rows, rows)

    def _fetch_messages(self, group_key: str) -> None:
        msgs = self.store.messages_in_group(
            group_key, inbox_only=self.inbox_only, limit=500
        )
        self.call_from_thread(self._show_messages, msgs)

    def _do_execute(self, actions: list[Action]) -> None:
        result = Executor(self.service, self.store).apply_all(actions)
        # Local cleanup: drop the acted ids so handled groups leave the view.
        # (They've left the inbox; a later sync re-indexes any archived/labeled
        # mail. Undo uses the actions_log, not these rows, so it still works.)
        for action, r in zip(actions, result.results):
            if r.error is None:
                self.store.delete_messages(
                    self.store.message_ids_in_group(action.group_key)
                )
        rows = self.store.group_counts(inbox_only=self.inbox_only)
        self.call_from_thread(self._set_rows, rows)
        self.call_from_thread(self._after_execute, result)

    def _do_sync(self) -> None:
        def emit(event: object) -> None:
            # Stream progress to the status line; ignore group snapshots (the
            # grouped view is refreshed once at the end).
            if isinstance(event, SyncProgress):
                self.call_from_thread(self._set_status, f"sync: {format_status(event)}")

        engine = SyncEngine(
            self.sync_service,
            self.store,
            emit=emit,
            batch_size=self.config.batch_size,
            cancel_event=self._cancel_event,
        )
        try:
            result = engine.incremental()
        except Exception as exc:  # surface failures rather than hanging silently
            self.call_from_thread(self._set_status, f"sync error: {exc}", True)
            self._syncing = False
            return

        rows = self.store.group_counts(inbox_only=self.inbox_only)
        self.call_from_thread(self._set_rows, rows)
        extras = []
        if result.removed:
            extras.append(f"{result.removed} removed")
        if result.errors:
            extras.append(f"{result.errors} errors")
        tail = f" ({', '.join(extras)})" if extras else ""
        self.call_from_thread(
            self._set_status, f"synced — {result.fetched} fetched{tail}", bool(result.errors)
        )
        self._syncing = False

    def _do_undo(self) -> None:
        result = Executor(self.service, self.store).undo_last(1)
        rows = self.store.group_counts(inbox_only=self.inbox_only)
        self.call_from_thread(self._set_rows, rows)
        modified = sum(r.modified for r in result.results)
        n = len(result.results)
        self.call_from_thread(
            self._set_status,
            f"undid {n} action(s) — {modified} messages restored"
            if n
            else "nothing to undo",
        )

    # -- UI-thread updates ---------------------------------------------------

    def _set_rows(self, rows: list[tuple]) -> None:
        # Remember which sender was highlighted so a refresh that drops rows
        # (execute/sync/undo) keeps the cursor on the *same* sender rather than
        # whatever now sits at the old row index.
        prev = self._current_row()
        prev_key = prev[1] if prev is not None else None
        self._rows = triage_view(rows, self.limit)
        self._render_senders(prefer_key=prev_key)
        if self._rows:
            self._fetch_messages_for_cursor()
        self._set_status(
            f"{len(self._rows)} groups · t trash · a archive · l label · "
            "k keep · f filter · ⏎ execute · s sync · u undo"
        )
        self._update_subtitle()

    def _render_senders(self, prefer_key: str | None = None) -> None:
        table = self.query_one("#senders", DataTable)
        cursor = table.cursor_row or 0
        table.clear()
        for row in self._rows:
            table.add_row(*sender_cells(row, self._plan.get(row[1])))
        if self._rows:
            # Prefer to restore the cursor onto the previously selected sender;
            # if it's gone (e.g. it was just executed), clamp the old index.
            target = cursor
            if prefer_key is not None:
                for i, row in enumerate(self._rows):
                    if row[1] == prefer_key:
                        target = i
                        break
            table.move_cursor(row=min(target, len(self._rows) - 1))

    def _show_messages(self, msgs: list[dict]) -> None:
        table = self.query_one("#messages", DataTable)
        table.clear()
        for msg in msgs:
            table.add_row(*message_cells(msg))

    def _after_execute(self, result: ApplyResult) -> None:
        self._plan.clear()
        msg = f"done — {result.modified} messages, {result.filters_created} filters created"
        if result.errors:
            msg += f", {result.errors} errors"
        self._set_status(msg, bool(result.errors))
        self._update_subtitle()

    def _set_status(self, text: str, error: bool = False) -> None:
        self.last_status = text
        status = self.query_one("#status", Static)
        status.set_class(error, "error")
        status.update(text)

    def _update_subtitle(self) -> None:
        planned = sum(1 for p in self._plan.values() if p.kind != "keep")
        scope = "inbox" if self.inbox_only else "all mail"
        self.sub_title = f"{scope} · {planned} planned"

    # -- selection -> right pane --------------------------------------------

    def _current_row(self) -> tuple | None:
        table = self.query_one("#senders", DataTable)
        idx = table.cursor_row
        if idx is None or not (0 <= idx < len(self._rows)):
            return None
        return self._rows[idx]

    def _fetch_messages_for_cursor(self) -> None:
        row = self._current_row()
        if row is not None:
            group_key = row[1]
            self.run_worker(
                lambda: self._fetch_messages(group_key),
                thread=True,
                exclusive=True,
                group="messages",
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "senders":
            self._fetch_messages_for_cursor()

    # -- actions (key bindings) ---------------------------------------------

    def action_plan(self, kind: str) -> None:
        row = self._current_row()
        if row is None:
            return
        group_key = row[1]
        if kind == "keep":
            self._plan.pop(group_key, None)
        else:
            # Default the filter on for archive/label, but OFF for trash: an
            # auto-trash filter silently deletes future mail, so it's opt-in (f).
            self._plan[group_key] = _Planned(
                kind=kind, create_filter=(kind != "trash")
            )
        self._render_senders()
        self._update_subtitle()

    def action_plan_label(self) -> None:
        row = self._current_row()
        if row is None:
            return
        group_key = row[1]

        def _done(name: str | None) -> None:
            if name:
                self._plan[group_key] = _Planned(
                    kind="label", label_name=name, create_filter=True
                )
                self._render_senders()
                self._update_subtitle()

        self.push_screen(_LabelPrompt(), _done)

    def action_toggle_filter(self) -> None:
        row = self._current_row()
        if row is None:
            return
        planned = self._plan.get(row[1])
        if planned is None or planned.kind == "keep":
            # Nothing to attach a filter to yet — tell the user instead of
            # silently doing nothing.
            self._set_status("choose an action first (t / a / l), then f toggles its filter")
            return
        planned.create_filter = not planned.create_filter
        self._render_senders()
        state = "on" if planned.create_filter else "off"
        self._set_status(f"future-mail filter {state} for {row[1]}")

    def _build_actions(self) -> list[Action]:
        by_key = {r[1]: r for r in self._rows}
        actions: list[Action] = []
        for group_key, planned in self._plan.items():
            if planned.kind == "keep":
                continue
            row = by_key.get(group_key)
            category = (row[0] if row else "personal") or "personal"
            actions.append(
                Action(
                    group_key=group_key,
                    category=category,
                    kind=planned.kind,
                    label_name=planned.label_name,
                    create_filter=planned.create_filter,
                )
            )
        return actions

    def action_execute(self) -> None:
        actions = self._build_actions()
        if not actions:
            self._set_status("nothing planned — press t / a / l on a sender first")
            return
        if self.service is None:
            self._set_status("no write service available", True)
            return
        self._set_status(f"executing {len(actions)} action(s)…")
        self.run_worker(lambda: self._do_execute(actions), thread=True, exclusive=True)

    def action_sync(self) -> None:
        if self._syncing:
            return
        if self.sync_service is None:
            self._set_status("no read service available for sync", True)
            return
        self._syncing = True
        self._set_status("syncing (incremental)…")
        self.run_worker(self._do_sync, thread=True, exclusive=True)

    def action_undo(self) -> None:
        if self.service is None:
            self._set_status("no write service available", True)
            return
        self._set_status("undoing last action…")
        self.run_worker(self._do_undo, thread=True, exclusive=True)

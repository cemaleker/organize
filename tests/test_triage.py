"""Tests for the triage TUI: pure display helpers + pilot smoke tests.

Mirrors test_tui.py: the cell-building / ranking helpers are pure and tested
directly, and a couple of Textual pilot tests drive the real load → plan →
execute → undo loop against the in-memory FakeService and a temp store.
"""

from inbox_cleaner.config import Config
from inbox_cleaner.events import STATE_FETCHING, STATE_RATE_LIMITED, SyncProgress
from inbox_cleaner.store import Store
from inbox_cleaner.triage import (
    TriageApp,
    _Planned,
    format_status,
    message_cells,
    plan_label,
    sender_cells,
    triage_view,
)
from textual.widgets import DataTable

from conftest import FakeService, make_message


async def _settle(app, pilot):
    """Wait for all workers to finish, tolerating exclusive-group cancellations.

    The right-pane message fetch runs in an exclusive ``messages`` group, so a
    superseded one is cancelled — harmless in the app, but ``wait_for_complete``
    re-raises it. Retry until a clean pass, then let the UI render.
    """
    for _ in range(40):
        try:
            await app.workers.wait_for_complete()
            break
        except Exception:
            await pilot.pause()
    await pilot.pause()


# (category, group_key, total, unread, oldest, newest)
_ROWS = [
    ("newsletter", "a.com", 10, 2, 100, 500),
    ("personal", "b.com", 30, 0, 50, 900),
    ("newsletter", "c.com", 5, 5, 10, 999),
]


# -- pure helpers -----------------------------------------------------------


def test_triage_view_ranks_noise_first_and_limits():
    out = triage_view(_ROWS)
    # newsletters (unsubscribable) above personal; within, more unread first.
    assert [r[1] for r in out] == ["c.com", "a.com", "b.com"]
    assert triage_view(_ROWS, limit=2) == out[:2]


def test_plan_label_renders_each_kind():
    assert plan_label(None) == ""
    assert plan_label(_Planned("trash")) == "TRASH"
    assert plan_label(_Planned("archive")) == "ARCHIVE"
    assert plan_label(_Planned("label", "Promos")) == "LABEL:Promos"
    assert plan_label(_Planned("keep")) == ""


def test_sender_cells_shows_plan_and_filter_mark():
    row = ("newsletter", "a.com", 10, 2, 100, 500)
    assert sender_cells(row, None) == ("", "a.com", "10", "2", "newsletter", "")
    cells = sender_cells(row, _Planned("trash", create_filter=True))
    assert cells[0] == "TRASH" and cells[5] == "✓"
    # filter mark off when create_filter is False
    assert sender_cells(row, _Planned("trash", create_filter=False))[5] == ""


def test_message_cells_marks_unread():
    msg = {"date": 1_700_000_000_000, "is_unread": True, "subject": "Hi"}
    assert message_cells(msg) == ("2023-11-14", "✉", "Hi")
    assert message_cells({"date": None, "is_unread": False, "subject": None}) == (
        "—",
        "",
        "—",
    )


def test_format_status_rate_limited_shows_countdown():
    ev = SyncProgress(fetched=10, total=100, state=STATE_RATE_LIMITED, retry_after=8.0)
    assert "retrying in 8s" in format_status(ev)
    assert "10/100" in format_status(ev)


def test_format_status_unknown_total():
    ev = SyncProgress(fetched=5, total=None, state=STATE_FETCHING)
    assert format_status(ev) == "fetching … 5/?"


# -- pilot smoke tests ------------------------------------------------------


def _seed_store(db, group_key, ids, *, category="newsletter", from_addr="news@x.com"):
    store = Store(db)
    store.connect()
    store.upsert_messages(
        [
            {
                "id": i,
                "from_addr": from_addr,
                "from_domain": from_addr.split("@")[-1],
                "subject": f"subject {i}",
                "date": 1000,
                "is_unread": True,
                "label_ids": ["INBOX", "UNREAD"],
                "snippet": "snip",
                "category": category,
                "group_key": group_key,
            }
            for i in ids
        ]
    )
    store.close()


async def test_load_populates_both_panes(tmp_path):
    db = tmp_path / "t.sqlite"
    _seed_store(db, "news@x.com", ["a", "b", "c"])
    app = TriageApp(Config(db_path=db), service=None)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.pause()
        assert app.query_one("#senders", DataTable).row_count == 1
        # right pane shows the group's three messages
        assert app.query_one("#messages", DataTable).row_count == 3


async def test_plan_then_execute_archives_and_clears(tmp_path):
    db = tmp_path / "t.sqlite"
    _seed_store(db, "news@x.com", ["a", "b"])
    svc = FakeService(pages={}, messages_by_id={}, profile={})
    app = TriageApp(Config(db_path=db), service=svc)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.pause()

        app.action_plan("archive")          # plan the highlighted (only) group
        await pilot.pause()
        assert app.query_one("#senders", DataTable).get_row_at(0)[0] == "ARCHIVE"

        app.action_execute()
        await _settle(app, pilot)
        await pilot.pause()

        # The backlog was archived (INBOX removed) and a filter created.
        modify = [b for k, b in svc.calls if k == "modify"]
        assert modify and modify[0]["removeLabelIds"] == ["INBOX"]
        assert len(svc.created_filters) == 1
        # Handled group left the view; plan cleared.
        assert app.query_one("#senders", DataTable).row_count == 0
        assert app._plan == {}
        assert "1 filters created" in app.last_status


async def test_execute_keeps_cursor_on_surviving_selected_sender(tmp_path):
    # Regression: with sender "mid" selected, executing actions on senders ranked
    # above it drops those rows so "mid" shifts up. The cursor must follow "mid",
    # not stay at its old row index (which now points at a different sender).
    db = tmp_path / "t.sqlite"
    # unread count drives newsletter ranking, so order is top > mid > bottom.
    _seed_store(db, "top.com", ["t1", "t2", "t3"], from_addr="n@top.com")
    _seed_store(db, "mid.com", ["m1", "m2"], from_addr="n@mid.com")
    _seed_store(db, "bot.com", ["b1"], from_addr="n@bot.com")
    svc = FakeService(pages={}, messages_by_id={}, profile={})
    app = TriageApp(Config(db_path=db), service=svc)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.pause()
        table = app.query_one("#senders", DataTable)
        assert [r[1] for r in app._rows] == ["top.com", "mid.com", "bot.com"]

        table.move_cursor(row=0)  # plan the *top* sender (the one above "mid")
        await pilot.pause()
        app.action_plan("archive")
        table.move_cursor(row=1)  # selection ends on the untouched "mid.com"
        await pilot.pause()
        assert app._rows[table.cursor_row][1] == "mid.com"

        app.action_execute()
        await _settle(app, pilot)
        await pilot.pause()

        # "top.com" is gone, "mid.com" moved up — cursor stays on "mid.com".
        assert [r[1] for r in app._rows] == ["mid.com", "bot.com"]
        assert app._rows[table.cursor_row][1] == "mid.com"


async def test_label_prompt_enter_submits_not_execute(tmp_path):
    # Regression: Enter in the label modal must submit the label, not trigger
    # the app's priority 'execute' binding. (check_action suppresses execute
    # while a modal is open so Enter falls through to the modal's Input.)
    db = tmp_path / "t.sqlite"
    _seed_store(db, "news@x.com", ["a", "b"])
    svc = FakeService(pages={}, messages_by_id={}, profile={})
    app = TriageApp(Config(db_path=db), service=svc)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.pause()

        app.action_plan_label()                       # open the label modal
        await pilot.pause()
        assert len(app.screen_stack) == 2             # modal is on top

        await pilot.press("p", "r", "o", "m", "o", "s")
        await pilot.press("enter")                    # should submit, not execute
        await _settle(app, pilot)
        await pilot.pause()

        # The label was recorded as a plan; nothing was executed against Gmail.
        assert len(app.screen_stack) == 1             # modal dismissed
        assert app._plan["news@x.com"].kind == "label"
        assert app._plan["news@x.com"].label_name == "promos"
        assert svc.calls == []


async def test_undo_restores_after_execute(tmp_path):
    db = tmp_path / "t.sqlite"
    _seed_store(db, "news@x.com", ["a", "b"])
    svc = FakeService(pages={}, messages_by_id={}, profile={})
    app = TriageApp(Config(db_path=db), service=svc)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.pause()
        app.action_plan("archive")  # archive creates a filter by default
        await pilot.pause()
        app.action_execute()
        await _settle(app, pilot)
        await pilot.pause()
        svc.calls.clear()

        app.action_undo()
        await _settle(app, pilot)
        await pilot.pause()

        # Undo reversed the delta in Gmail and deleted the filter.
        modify = [b for k, b in svc.calls if k == "modify"]
        assert modify[0]["addLabelIds"] == ["INBOX"]
        assert svc.deleted_filters == ["filter_1"]
        assert "restored" in app.last_status


async def test_trash_does_not_create_a_filter_by_default(tmp_path):
    db = tmp_path / "t.sqlite"
    _seed_store(db, "spam@x.com", ["a"])
    svc = FakeService(pages={}, messages_by_id={}, profile={})
    app = TriageApp(Config(db_path=db), service=svc)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.pause()
        app.action_plan("trash")
        await pilot.pause()
        # No filter mark on the row, and none created on execute.
        assert app.query_one("#senders", DataTable).get_row_at(0)[5] == ""
        app.action_execute()
        await _settle(app, pilot)
        await pilot.pause()
        assert svc.created_filters == []
        assert [b for k, b in svc.calls if k == "modify"][0]["addLabelIds"] == ["TRASH"]


async def test_f_toggles_filter_and_reports_state(tmp_path):
    db = tmp_path / "t.sqlite"
    _seed_store(db, "news@x.com", ["a"])
    app = TriageApp(Config(db_path=db), service=None)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.pause()

        # f before any action: gives guidance instead of silently no-op'ing.
        app.action_toggle_filter()
        await pilot.pause()
        assert "choose an action first" in app.last_status

        app.action_plan("archive")  # filter on by default -> ✓
        await pilot.pause()
        assert app.query_one("#senders", DataTable).get_row_at(0)[5] == "✓"

        app.action_toggle_filter()  # turn it off
        await pilot.pause()
        assert app.query_one("#senders", DataTable).get_row_at(0)[5] == ""
        assert "filter off" in app.last_status


async def test_sync_action_refreshes_index(tmp_path):
    # Empty store; the 's' action runs an incremental sync (which falls back to a
    # backfill with no stored history) and pulls a new message into the view.
    db = tmp_path / "t.sqlite"
    msgs = {
        "n1": make_message(
            "n1",
            from_="new@x.com",
            subject="Fresh",
            extra_headers={"List-Unsubscribe": "<mailto:u@x.com>"},
        )
    }
    sync_svc = FakeService(
        pages={None: {"messages": [{"id": "n1"}]}},
        messages_by_id=msgs,
        profile={"messagesTotal": 1, "historyId": "100"},
    )
    app = TriageApp(Config(db_path=db), service=None, sync_service=sync_svc)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.pause()
        assert app.query_one("#senders", DataTable).row_count == 0

        app.action_sync()
        await _settle(app, pilot)
        await pilot.pause()

        assert app.query_one("#senders", DataTable).row_count == 1
        assert "synced — 1 fetched" in app.last_status


async def test_sync_action_without_service_reports(tmp_path):
    app = TriageApp(Config(db_path=tmp_path / "t.sqlite"), service=None, sync_service=None)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.pause()
        app.action_sync()
        await pilot.pause()
        assert "no read service" in app.last_status


async def test_activity_indicator_animates_while_busy_then_clears(tmp_path):
    from inbox_cleaner.triage import _SPINNER_FRAMES

    db = tmp_path / "t.sqlite"
    _seed_store(db, "news@x.com", ["a"])
    app = TriageApp(Config(db_path=db), service=None)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.pause()

        status = app.query_one("#status")
        # Idle: the status line carries no spinner prefix, and last_status mirrors
        # exactly what's painted.
        assert not app._busy
        assert str(status.render()) == app.last_status

        # While busy a spinner frame is prefixed, but last_status stays clean so
        # callers/tests still see just the message text.
        app._start_spinner()
        await pilot.pause()
        rendered = str(status.render())
        assert rendered.startswith(tuple(_SPINNER_FRAMES))
        assert app.last_status in rendered
        assert app.last_status == app._status_text

        # Stopping clears the prefix again.
        app._stop_spinner()
        await pilot.pause()
        assert str(app.query_one("#status").render()) == app.last_status


async def test_activity_indicator_ignores_message_fetch_worker(tmp_path):
    """Navigating senders (the ``messages`` group) must not trip the spinner."""
    from types import SimpleNamespace

    from inbox_cleaner.triage import WorkerState

    db = tmp_path / "t.sqlite"
    _seed_store(db, "news@x.com", ["a"])
    app = TriageApp(Config(db_path=db), service=None)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.pause()

        event = SimpleNamespace(
            worker=SimpleNamespace(group="messages"), state=WorkerState.RUNNING
        )
        app.on_worker_state_changed(event)  # type: ignore[arg-type]
        assert not app._busy


async def test_execute_without_plan_is_a_no_op(tmp_path):
    db = tmp_path / "t.sqlite"
    _seed_store(db, "news@x.com", ["a"])
    svc = FakeService(pages={}, messages_by_id={}, profile={})
    app = TriageApp(Config(db_path=db), service=svc)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        await pilot.pause()
        app.action_execute()
        await pilot.pause()
        assert "nothing planned" in app.last_status
        assert svc.calls == []

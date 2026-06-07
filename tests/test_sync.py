"""Headless tests for the sync engine, driven by the fake Gmail service."""

import threading

import pytest

from inbox_cleaner.events import (
    STATE_RATE_LIMITED,
    GroupSnapshot,
    SyncDone,
    SyncProgress,
)
from inbox_cleaner.store import Store
from inbox_cleaner.sync import SyncEngine
from conftest import FakeService, make_message


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "sync.sqlite")
    s.connect()
    yield s
    s.close()


def _two_page_service():
    """Two pages of two messages each; second page is the last."""
    msgs = {mid: make_message(mid) for mid in ("m1", "m2", "m3", "m4")}
    pages = {
        None: {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "p1"},
        "p1": {"messages": [{"id": "m3"}, {"id": "m4"}]},
    }
    profile = {"messagesTotal": 4, "historyId": "9001"}
    return FakeService(pages, msgs, profile)


def _no_sleep(_seconds):
    pass


def test_backfill_ingests_all_pages(store):
    engine = SyncEngine(_two_page_service(), store, sleep=_no_sleep)
    done = engine.backfill()

    assert isinstance(done, SyncDone)
    assert done.fetched == 4
    assert done.errors == 0
    count = store.connect().execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 4


def test_backfill_records_sync_state_on_finish(store):
    SyncEngine(_two_page_service(), store, sleep=_no_sleep).backfill()
    state = store.get_sync_state()
    assert state["history_id"] == "9001"
    assert state["last_full_sync"] is not None


def test_fields_and_classification_are_stored(store):
    svc = FakeService(
        pages={None: {"messages": [{"id": "m1"}]}},
        messages_by_id={
            "m1": make_message(
                "m1", from_="Bob <bob@news.example.com>", labels=["INBOX"], date_ms=555
            )
        },
        profile={"messagesTotal": 1, "historyId": "1"},
    )
    SyncEngine(svc, store, sleep=_no_sleep).backfill()
    row = store.connect().execute(
        "SELECT from_addr, from_domain, is_unread, date, category, group_key "
        "FROM messages WHERE id='m1'"
    ).fetchone()
    assert row["from_addr"] == "bob@news.example.com"
    assert row["from_domain"] == "news.example.com"
    assert row["is_unread"] == 0  # no UNREAD label
    assert row["date"] == 555
    # Plain mail with no list/bulk headers -> personal, grouped by full address.
    assert row["category"] == "personal"
    assert row["group_key"] == "bob@news.example.com"


def test_smart_skip_avoids_refetching_indexed_ids(store):
    # Mail already in the index is not re-fetched; a re-run only pulls what's new.
    store.upsert_messages([{"id": "m1", "group_key": "x"}, {"id": "m2", "group_key": "x"}])
    done = SyncEngine(_two_page_service(), store, sleep=_no_sleep).backfill()
    # m1/m2 were skipped; only m3/m4 fetched.
    assert done.fetched == 2
    assert done.skipped == 2
    ids = {
        r["id"]
        for r in store.connect().execute("SELECT id FROM messages").fetchall()
    }
    assert ids == {"m1", "m2", "m3", "m4"}


def test_rate_limit_is_retried_and_emitted(store):
    svc = _two_page_service()
    svc.batch_failures = 2  # first batch fails twice, then succeeds
    events = []
    engine = SyncEngine(svc, store, emit=events.append, sleep=_no_sleep)
    done = engine.backfill()

    assert done.fetched == 4
    assert any(
        isinstance(e, SyncProgress) and e.state == STATE_RATE_LIMITED for e in events
    )


def test_connection_reset_on_list_is_retried(store):
    # A transport error (not an HttpError) on a list page recovers via backoff
    # instead of escaping the sync engine.
    svc = _two_page_service()
    svc.list_raises = [ConnectionResetError(104, "Connection reset by peer")]
    events = []
    done = SyncEngine(svc, store, emit=events.append, sleep=_no_sleep).backfill()

    assert done.fetched == 4
    assert done.errors == 0
    assert any(
        isinstance(e, SyncProgress) and e.state == STATE_RATE_LIMITED for e in events
    )


def test_batch_member_errors_are_counted(store):
    # m2 has no fixture -> the batch reports a per-message 404.
    svc = FakeService(
        pages={None: {"messages": [{"id": "m1"}, {"id": "m2"}]}},
        messages_by_id={"m1": make_message("m1")},
        profile={"messagesTotal": 2, "historyId": "1"},
    )
    done = SyncEngine(svc, store, sleep=_no_sleep).backfill()
    assert done.fetched == 1
    assert done.errors == 1


def test_batch_member_errors_are_broken_down_by_reason(store):
    # m2 and m3 have no fixture -> two per-message 404s, captured by cause so a
    # large error count can be diagnosed instead of being a bare number.
    svc = FakeService(
        pages={None: {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]}},
        messages_by_id={"m1": make_message("m1")},
        profile={"messagesTotal": 3, "historyId": "1"},
    )
    done = SyncEngine(svc, store, sleep=_no_sleep).backfill()
    assert done.errors == 2
    assert done.error_reasons == {"404 notFound": 2}
    assert sum(done.error_reasons.values()) == done.errors


def test_member_rate_limit_is_retried_during_sync(store):
    # m2's sub-request is throttled twice, then succeeds. It must be re-fetched
    # in-place and end up indexed — not dropped and reported as an error.
    svc = FakeService(
        pages={None: {"messages": [{"id": "m1"}, {"id": "m2"}]}},
        messages_by_id={"m1": make_message("m1"), "m2": make_message("m2")},
        profile={"messagesTotal": 2, "historyId": "1"},
    )
    svc.member_failures = {"m2": 2}
    events = []
    done = SyncEngine(svc, store, emit=events.append, sleep=_no_sleep).backfill()

    assert done.fetched == 2
    assert done.errors == 0
    assert done.error_reasons == {}
    # A rate-limit progress event is emitted before each in-batch backoff.
    assert any(
        isinstance(e, SyncProgress) and e.state == STATE_RATE_LIMITED for e in events
    )
    ids = {r["id"] for r in store.connect().execute("SELECT id FROM messages")}
    assert ids == {"m1", "m2"}


def test_member_rate_limit_exhausts_attempts_then_counted(store):
    # m2 never recovers; after max_attempts it is finally counted as an error.
    svc = FakeService(
        pages={None: {"messages": [{"id": "m1"}, {"id": "m2"}]}},
        messages_by_id={"m1": make_message("m1"), "m2": make_message("m2")},
        profile={"messagesTotal": 2, "historyId": "1"},
    )
    svc.member_failures = {"m2": 99}  # always throttled
    done = SyncEngine(svc, store, sleep=_no_sleep, max_attempts=3).backfill()

    assert done.fetched == 1
    assert done.errors == 1
    assert done.error_reasons == {"429 rateLimitExceeded": 1}


def test_member_permanent_error_is_not_retried(store):
    # A 404 is permanent — counted immediately, with only one batch attempt.
    svc = FakeService(
        pages={None: {"messages": [{"id": "m1"}, {"id": "m2"}]}},
        messages_by_id={"m1": make_message("m1")},  # m2 missing -> 404
        profile={"messagesTotal": 2, "historyId": "1"},
    )
    done = SyncEngine(svc, store, sleep=_no_sleep).backfill()

    assert done.fetched == 1
    assert done.errors == 1
    assert done.error_reasons == {"404 notFound": 2 - 1}  # one missing message
    # No retry: the page's ids were batched exactly once.
    assert svc.batch_sizes == [2]


def test_group_snapshots_emitted(store):
    events = []
    engine = SyncEngine(
        _two_page_service(), store, emit=events.append, snapshot_every=2, sleep=_no_sleep
    )
    engine.backfill()
    assert any(isinstance(e, GroupSnapshot) for e in events)


# -- smart fetch / prune / trash --------------------------------------------


def test_backfill_skips_already_indexed(store):
    # m1, m2 already present -> only m3, m4 should be fetched.
    store.upsert_messages(
        [{"id": "m1", "group_key": "x"}, {"id": "m2", "group_key": "x"}]
    )
    svc = _two_page_service()
    done = SyncEngine(svc, store, sleep=_no_sleep).backfill()

    assert done.fetched == 2
    assert done.skipped == 2
    # Only the two new ids were actually fetched over the wire.
    assert sum(svc.batch_sizes) == 2
    count = store.connect().execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 4


def test_backfill_full_refetches_everything(store):
    store.upsert_messages([{"id": "m1", "group_key": "x"}])
    svc = _two_page_service()
    done = SyncEngine(svc, store, sleep=_no_sleep, full=True).backfill()

    assert done.skipped == 0
    assert sum(svc.batch_sizes) == 4  # all four fetched despite m1 present


def test_backfill_prunes_locally_removed_mail(store):
    # 'stale' was indexed before but is no longer in the mailbox listing.
    store.upsert_messages([{"id": "stale", "group_key": "x"}])
    done = SyncEngine(_two_page_service(), store, sleep=_no_sleep).backfill()

    assert done.removed == 1
    ids = {r["id"] for r in store.connect().execute("SELECT id FROM messages")}
    assert ids == {"m1", "m2", "m3", "m4"}  # stale pruned


def test_backfill_does_not_prune_with_query(store):
    store.upsert_messages([{"id": "stale", "group_key": "x"}])
    # A filtered backfill is a partial view; pruning would wrongly delete.
    svc = FakeService(
        pages={None: {"messages": [{"id": "m1"}]}},
        messages_by_id={"m1": make_message("m1")},
        profile={"messagesTotal": 1, "historyId": "1"},
    )
    done = SyncEngine(svc, store, sleep=_no_sleep, query="in:inbox").backfill()
    assert done.removed == 0
    ids = {r["id"] for r in store.connect().execute("SELECT id FROM messages")}
    assert "stale" in ids


def test_backfill_excludes_trashed_messages(store):
    svc = FakeService(
        pages={None: {"messages": [{"id": "good"}, {"id": "trash"}]}},
        messages_by_id={
            "good": make_message("good"),
            "trash": make_message("trash", labels=["INBOX", "TRASH"]),
        },
        profile={"messagesTotal": 2, "historyId": "1"},
    )
    done = SyncEngine(svc, store, sleep=_no_sleep).backfill()
    assert done.fetched == 1  # only 'good' indexed
    ids = {r["id"] for r in store.connect().execute("SELECT id FROM messages")}
    assert ids == {"good"}


def test_incremental_removes_message_moved_to_trash(store):
    # m1 indexed; a labelsAdded(TRASH) arrives -> it should be removed locally.
    store.upsert_messages([{"id": "m1", "group_key": "x"}])
    store.set_sync_state(history_id="100")
    svc = FakeService(
        pages={},
        messages_by_id={"m1": make_message("m1", labels=["TRASH"])},
        profile={"messagesTotal": 0, "historyId": "200"},
    )
    svc.history_pages = {
        None: {
            "history": [
                {"labelsAdded": [{"message": {"id": "m1"}, "labelIds": ["TRASH"]}]}
            ],
            "historyId": "200",
        }
    }
    done = SyncEngine(svc, store, sleep=_no_sleep).incremental()
    assert done.removed == 1
    count = store.connect().execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 0


# -- incremental sync -------------------------------------------------------


def test_incremental_applies_added_and_deleted(store):
    # Existing state: m1 already indexed, cursor at history 100.
    store.upsert_messages([{"id": "m1", "category": "personal", "group_key": "x"}])
    store.set_sync_state(history_id="100")

    svc = FakeService(
        pages={},
        messages_by_id={"m2": make_message("m2")},
        profile={"messagesTotal": 1, "historyId": "200"},
    )
    svc.history_pages = {
        None: {
            "history": [
                {"messagesAdded": [{"message": {"id": "m2"}}]},
                {"messagesDeleted": [{"message": {"id": "m1"}}]},
            ],
            "historyId": "200",
        }
    }
    done = SyncEngine(svc, store, sleep=_no_sleep).incremental()

    ids = {r["id"] for r in store.connect().execute("SELECT id FROM messages")}
    assert ids == {"m2"}  # m1 deleted, m2 added
    assert done.fetched == 1
    assert store.get_sync_state()["history_id"] == "200"


def test_incremental_label_change_refreshes_message(store):
    # m1 is currently unread; a labelsRemoved(UNREAD) record arrives.
    store.upsert_messages(
        [{"id": "m1", "is_unread": True, "category": "personal", "group_key": "x"}]
    )
    store.set_sync_state(history_id="100")

    svc = FakeService(
        pages={},
        messages_by_id={"m1": make_message("m1", labels=["INBOX"])},  # no UNREAD now
        profile={"messagesTotal": 1, "historyId": "201"},
    )
    svc.history_pages = {
        None: {
            "history": [
                {
                    "labelsRemoved": [
                        {"message": {"id": "m1"}, "labelIds": ["UNREAD"]}
                    ]
                }
            ],
            "historyId": "201",
        }
    }
    SyncEngine(svc, store, sleep=_no_sleep).incremental()

    is_unread = store.connect().execute(
        "SELECT is_unread FROM messages WHERE id='m1'"
    ).fetchone()["is_unread"]
    assert is_unread == 0  # refreshed from re-fetched metadata


def test_incremental_404_falls_back_to_backfill(store):
    store.set_sync_state(history_id="1")  # stale cursor
    svc = _two_page_service()
    svc.history_404 = True

    done = SyncEngine(svc, store, sleep=_no_sleep).incremental()
    # Backfill ran instead: all four messages ingested.
    assert done.fetched == 4
    count = store.connect().execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 4


def test_incremental_without_cursor_does_full_backfill(store):
    # No prior history_id -> first run must be a full backfill.
    done = SyncEngine(_two_page_service(), store, sleep=_no_sleep).incremental()
    assert done.fetched == 4


def test_incremental_chunks_large_change_set(store):
    # Many changed ids must be split across batches (Gmail caps a batch at 1000).
    msgs = {f"m{i}": make_message(f"m{i}") for i in range(5)}
    store.set_sync_state(history_id="100")
    svc = FakeService(
        pages={},
        messages_by_id=msgs,
        profile={"messagesTotal": 5, "historyId": "200"},
    )
    svc.history_pages = {
        None: {
            "history": [
                {"messagesAdded": [{"message": {"id": f"m{i}"}}]} for i in range(5)
            ],
            "historyId": "200",
        }
    }
    done = SyncEngine(svc, store, sleep=_no_sleep, batch_size=2).incremental()

    assert done.fetched == 5
    # 5 ids at batch_size 2 -> chunks of [2, 2, 1]; none exceeds the limit.
    assert max(svc.batch_sizes) <= 2
    assert sum(svc.batch_sizes) == 5


# -- graceful cancel --------------------------------------------------------


def test_cancel_before_start_fetches_nothing(store):
    cancel = threading.Event()
    cancel.set()
    done = SyncEngine(
        _two_page_service(), store, sleep=_no_sleep, cancel_event=cancel
    ).backfill()
    assert done.cancelled is True
    assert done.fetched == 0


def test_cancel_midway_is_graceful(store):
    cancel = threading.Event()

    # Trip the cancel once the first page's batch has been written.
    def emit(event):
        if isinstance(event, SyncProgress) and event.fetched >= 2:
            cancel.set()

    done = SyncEngine(
        _two_page_service(), store, emit=emit, sleep=_no_sleep, cancel_event=cancel
    ).backfill()

    assert done.cancelled is True
    assert done.fetched == 2
    # A cancelled run is incomplete, so it persists no sync cursor.
    assert store.get_sync_state() is None

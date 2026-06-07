"""Tests for the executor — the write path.

All offline, against the in-memory :class:`FakeService` and a temp-file store.
Covers the three reversible actions (trash / archive / label), label resolution
and creation, filter creation (sender vs mailing-list criteria), the
log-before-act contract, undo, and rate-limit retry.
"""

import pytest

from inbox_cleaner.executor import Action, Executor
from inbox_cleaner.store import Store

from conftest import FakeService


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "exec.sqlite")
    s.connect()
    yield s
    s.close()


@pytest.fixture
def service():
    return FakeService(pages={}, messages_by_id={}, profile={})


def _seed(store, group_key, ids, *, category="newsletter", from_addr="news@x.com"):
    store.upsert_messages(
        [
            {
                "id": i,
                "thread_id": "t",
                "from_addr": from_addr,
                "from_domain": from_addr.split("@")[-1],
                "subject": "s",
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


def _modify_calls(service):
    return [body for kind, body in service.calls if kind == "modify"]


def test_archive_removes_inbox_and_creates_filter(store, service):
    _seed(store, "news@x.com", ["a", "b"], from_addr="news@x.com")
    ex = Executor(service, store)

    res = ex.apply(Action(group_key="news@x.com", category="newsletter", kind="archive"))

    assert res.matched == 2
    assert res.modified == 2
    body = _modify_calls(service)[0]
    assert body["removeLabelIds"] == ["INBOX"]
    assert "addLabelIds" not in body
    assert sorted(body["ids"]) == ["a", "b"]
    # Filter created on the sender.
    assert service.created_filters[0]["criteria"] == {"from": "news@x.com"}
    assert service.created_filters[0]["action"] == {"removeLabelIds": ["INBOX"]}
    assert res.filter_id == "filter_1"


def test_trash_adds_trash_label(store, service):
    _seed(store, "spam@x.com", ["a"], from_addr="spam@x.com")
    ex = Executor(service, store)

    res = ex.apply(Action(group_key="spam@x.com", kind="trash", create_filter=False))

    assert _modify_calls(service)[0]["addLabelIds"] == ["TRASH"]
    assert res.filter_id is None
    assert not service.created_filters


def test_label_resolves_creates_and_archives(store, service):
    _seed(store, "promo@x.com", ["a", "b", "c"], from_addr="promo@x.com")
    ex = Executor(service, store)

    res = ex.apply(
        Action(group_key="promo@x.com", kind="label", label_name="Promos")
    )

    # The label was created (didn't exist), then applied + INBOX removed.
    assert service.labels[0]["name"] == "Promos"
    label_id = service.labels[0]["id"]
    body = _modify_calls(service)[0]
    assert body["addLabelIds"] == [label_id]
    assert body["removeLabelIds"] == ["INBOX"]
    assert res.modified == 3


def test_label_reuses_existing(store, service):
    service.labels.append({"id": "Label_existing", "name": "Promos"})
    _seed(store, "promo@x.com", ["a"], from_addr="promo@x.com")
    ex = Executor(service, store)

    ex.apply(Action(group_key="promo@x.com", kind="label", label_name="Promos"))

    # No new label created; the existing id is reused.
    assert len(service.labels) == 1
    assert _modify_calls(service)[0]["addLabelIds"] == ["Label_existing"]


def test_mailing_list_filter_matches_list_id(store, service):
    _seed(store, "dev.list.x.com", ["a"], category="mailing_list", from_addr="a@x.com")
    ex = Executor(service, store)

    ex.apply(Action(group_key="dev.list.x.com", category="mailing_list", kind="archive"))

    assert service.created_filters[0]["criteria"] == {"query": "list:dev.list.x.com"}


def test_action_is_logged_before_acting_with_inverse(store, service):
    _seed(store, "news@x.com", ["a", "b"], from_addr="news@x.com")
    ex = Executor(service, store)

    ex.apply(Action(group_key="news@x.com", category="newsletter", kind="archive"))

    actions = store.recent_actions()
    assert len(actions) == 1
    state = actions[0]["prior_state"]
    assert sorted(state["ids"]) == ["a", "b"]
    assert state["undo_add"] == ["INBOX"]      # archive's inverse re-adds INBOX
    assert state["undo_remove"] == []
    assert state["filter_id"] == "filter_1"    # backfilled after create


def test_undo_reverses_delta_and_deletes_filter(store, service):
    _seed(store, "news@x.com", ["a", "b"], from_addr="news@x.com")
    ex = Executor(service, store)
    ex.apply(Action(group_key="news@x.com", category="newsletter", kind="trash"))
    service.calls.clear()

    res = ex.undo_last()

    assert res.results[0].modified == 2
    body = _modify_calls(service)[0]
    assert body["addLabelIds"] == ["INBOX"]    # un-trash + restore inbox
    assert body["removeLabelIds"] == ["TRASH"]
    assert service.deleted_filters == ["filter_1"]
    assert store.recent_actions() == []        # log row removed


def test_filter_only_when_no_backlog(store, service):
    # No messages indexed for this group: still create the future filter.
    ex = Executor(service, store)
    res = ex.apply(Action(group_key="future@x.com", category="newsletter", kind="archive"))

    assert res.matched == 0
    assert res.modified == 0
    assert not _modify_calls(service)          # nothing to batchModify
    assert res.filter_id == "filter_1"


def test_batch_modify_chunks_over_1000(store, service):
    ids = [f"m{i}" for i in range(2500)]
    _seed(store, "big@x.com", ids, from_addr="big@x.com")
    ex = Executor(service, store)

    res = ex.apply(Action(group_key="big@x.com", kind="archive", create_filter=False))

    sizes = [len(b["ids"]) for b in _modify_calls(service)]
    assert sizes == [1000, 1000, 500]
    assert res.modified == 2500


def test_rate_limit_is_retried(store, service):
    _seed(store, "news@x.com", ["a"], from_addr="news@x.com")
    service.call_failures["modify"] = 1  # fail once, then succeed
    ex = Executor(service, store, sleep=lambda _s: None)

    res = ex.apply(Action(group_key="news@x.com", kind="archive", create_filter=False))

    assert res.error is None
    assert res.modified == 1


def test_connection_reset_is_retried(store, service):
    # A transient transport error (not an HttpError) recovers via backoff
    # instead of crashing the caller.
    _seed(store, "news@x.com", ["a"], from_addr="news@x.com")
    service.call_raises["modify"] = [
        ConnectionResetError(104, "Connection reset by peer")
    ]
    ex = Executor(service, store, sleep=lambda _s: None)

    res = ex.apply(Action(group_key="news@x.com", kind="archive", create_filter=False))

    assert res.error is None
    assert res.modified == 1


def test_persistent_connection_reset_is_recorded_not_raised(store, service):
    # If the transport error never clears, the action surfaces as a recorded
    # error rather than propagating out of apply() (which would crash the TUI's
    # thread worker). The action stays in the log so it remains undoable.
    _seed(store, "news@x.com", ["a"], from_addr="news@x.com")
    service.call_raises["modify"] = [
        ConnectionResetError(104, "Connection reset by peer") for _ in range(20)
    ]
    ex = Executor(service, store, max_attempts=3, sleep=lambda _s: None)

    res = ex.apply(Action(group_key="news@x.com", kind="archive", create_filter=False))

    assert res.modified == 0
    assert res.error is not None
    assert "Connection reset by peer" in res.error
    assert len(store.recent_actions()) == 1

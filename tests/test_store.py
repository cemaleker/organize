"""Tests for the SQLite store.

All offline, against a temp-file database (FTS5 + WAL behave like the real
thing). Covers migrations, idempotent upserts with field refresh, the senders
view, sync-state round-tripping, and FTS search staying in sync via triggers.
"""

import json

import pytest

from inbox_cleaner.classify import classify
from inbox_cleaner.store import SCHEMA_VERSION, Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.sqlite")
    s.connect()
    yield s
    s.close()


def _msg(id_, **over):
    base = dict(
        id=id_,
        thread_id="t1",
        from_addr="alice@example.com",
        from_domain="example.com",
        subject="Hello",
        date=1000,
        size_estimate=2048,
        is_unread=True,
        label_ids=["INBOX", "UNREAD"],
        snippet="hi there",
        category="personal",
        group_key="example.com",
    )
    base.update(over)
    return base


def test_migration_sets_user_version(store):
    v = store.connect().execute("PRAGMA user_version").fetchone()[0]
    assert v == SCHEMA_VERSION


def test_migration_is_idempotent_across_reconnect(tmp_path):
    path = tmp_path / "x.sqlite"
    Store(path).connect()  # creates + migrates
    s2 = Store(path)
    conn = s2.connect()  # should no-op the migration, not error
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    s2.close()


def test_upsert_inserts_and_encodes_labels(store):
    n = store.upsert_messages([_msg("m1")])
    assert n == 1
    row = store.connect().execute(
        "SELECT label_ids, is_unread FROM messages WHERE id='m1'"
    ).fetchone()
    assert json.loads(row["label_ids"]) == ["INBOX", "UNREAD"]
    assert row["is_unread"] == 1


def test_upsert_refreshes_mutable_fields(store):
    store.upsert_messages([_msg("m1", is_unread=True, category="personal")])
    # Re-ingest same id with changed labels/read-state/category.
    store.upsert_messages(
        [_msg("m1", is_unread=False, category="newsletter", subject="Changed")]
    )
    row = store.connect().execute(
        "SELECT is_unread, category, subject FROM messages WHERE id='m1'"
    ).fetchone()
    assert row["is_unread"] == 0
    assert row["category"] == "newsletter"
    assert row["subject"] == "Changed"
    # Still exactly one row — upsert, not duplicate insert.
    count = store.connect().execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 1


def test_upsert_requires_id(store):
    with pytest.raises(ValueError):
        store.upsert_messages([{"subject": "no id"}])


def test_senders_view_aggregates(store):
    store.upsert_messages(
        [
            _msg("m1", from_addr="a@x.com", is_unread=True, date=100),
            _msg("m2", from_addr="a@x.com", is_unread=False, date=300),
            _msg("m3", from_addr="b@y.com", is_unread=True, date=200),
        ]
    )
    rows = {
        r["from_addr"]: r
        for r in store.connect().execute(
            "SELECT from_addr, total, unread, first_seen, last_seen FROM senders"
        )
    }
    assert rows["a@x.com"]["total"] == 2
    assert rows["a@x.com"]["unread"] == 1
    assert rows["a@x.com"]["first_seen"] == 100
    assert rows["a@x.com"]["last_seen"] == 300
    assert rows["b@y.com"]["total"] == 1


def test_class_headers_stored_as_json(store):
    store.upsert_messages(
        [_msg("m1", class_headers={"From": "a@x.com", "List-Id": "<l.x.com>"})]
    )
    raw = store.connect().execute(
        "SELECT class_headers FROM messages WHERE id='m1'"
    ).fetchone()["class_headers"]
    assert json.loads(raw) == {"From": "a@x.com", "List-Id": "<l.x.com>"}


def test_reclassify_updates_from_stored_headers(store):
    # Ingested with a wrong category but the real headers are persisted.
    store.upsert_messages(
        [
            _msg(
                "m1",
                category="uncategorized",
                group_key="x.com",
                class_headers={
                    "From": "noreply@github.com",
                    "List-Id": "Repo <repo.github.com>",
                },
            )
        ]
    )
    updated, skipped = store.reclassify_all(classify)
    assert (updated, skipped) == (1, 0)
    row = store.connect().execute(
        "SELECT category, group_key FROM messages WHERE id='m1'"
    ).fetchone()
    assert row["category"] == "mailing_list"
    assert row["group_key"] == "repo.github.com"


def test_reclassify_skips_rows_without_headers(store):
    store.upsert_messages([_msg("m1", class_headers=None)])  # legacy row
    store.upsert_messages(
        [_msg("m2", class_headers={"From": "x@y.com", "List-Unsubscribe": "<u>"})]
    )
    updated, skipped = store.reclassify_all(classify)
    assert updated == 1
    assert skipped == 1
    cat = store.connect().execute(
        "SELECT category FROM messages WHERE id='m2'"
    ).fetchone()["category"]
    assert cat == "newsletter"


def test_existing_ids_returns_present_subset(store):
    store.upsert_messages([_msg("m1"), _msg("m2")])
    assert store.existing_ids(["m1", "m3", "m2"]) == {"m1", "m2"}
    assert store.existing_ids([]) == set()


def test_prune_missing_deletes_absent_rows(store):
    store.upsert_messages([_msg("keep"), _msg("gone1"), _msg("gone2")])
    removed = store.prune_missing(["keep", "other"])
    assert removed == 2
    ids = {r["id"] for r in store.connect().execute("SELECT id FROM messages")}
    assert ids == {"keep"}


def test_sender_stats_recency_order_and_unsub_flag(store):
    store.upsert_messages(
        [
            _msg("m1", group_key="news.com", category="newsletter", date=100),
            _msg("m2", group_key="news.com", category="newsletter", date=400),
            _msg("m3", group_key="friend.com", category="personal", date=300),
        ]
    )
    rows = store.sender_stats(by="group", order="recency")
    # newest first: news.com (max date 400) before friend.com (300)
    assert [r["key"] for r in rows] == ["news.com", "friend.com"]
    by_key = {r["key"]: r for r in rows}
    assert by_key["news.com"]["total"] == 2
    assert by_key["news.com"]["unsub"] == 1   # newsletter
    assert by_key["friend.com"]["unsub"] == 0  # personal


def test_sender_stats_noise_order_prioritizes_unsub_unread(store):
    store.upsert_messages(
        [
            # personal, lots of unread, but not unsubscribable
            _msg("p1", group_key="boss.com", category="personal", is_unread=True),
            _msg("p2", group_key="boss.com", category="personal", is_unread=True),
            # newsletter, unread -> should rank first in noise mode
            _msg("n1", group_key="promo.com", category="newsletter", is_unread=True),
        ]
    )
    rows = store.sender_stats(by="group", order="noise")
    assert rows[0]["key"] == "promo.com"  # unsub + unread wins


def test_sender_stats_window_filters_old_mail(store):
    store.upsert_messages(
        [
            _msg("old", group_key="a.com", date=1000),
            _msg("new", group_key="b.com", date=5000),
        ]
    )
    rows = store.sender_stats(since_ms=4000)
    assert [r["key"] for r in rows] == ["b.com"]


def test_sender_stats_inbox_only_excludes_archived(store):
    store.upsert_messages(
        [
            _msg("keep", group_key="a.com", label_ids=["INBOX", "UNREAD"]),
            # moved/archived: no INBOX label -> excluded when inbox_only
            _msg("moved", group_key="b.com", label_ids=["UNREAD"]),
        ]
    )
    rows = store.sender_stats(by="group", inbox_only=True)
    assert [r["key"] for r in rows] == ["a.com"]
    # without the filter, both show up
    all_rows = store.sender_stats(by="group")
    assert {r["key"] for r in all_rows} == {"a.com", "b.com"}


def test_sender_stats_by_domain_and_limit(store):
    store.upsert_messages(
        [
            _msg("m1", from_domain="x.com", group_key="g1", date=10),
            _msg("m2", from_domain="x.com", group_key="g2", date=20),
            _msg("m3", from_domain="y.com", group_key="g3", date=30),
        ]
    )
    rows = store.sender_stats(by="domain", order="count", limit=1)
    assert len(rows) == 1
    assert rows[0]["key"] == "x.com"  # two messages
    assert rows[0]["total"] == 2


def test_sync_state_round_trip(store):
    assert store.get_sync_state() is None
    store.set_sync_state(history_id="42", last_full_sync=999)
    state = store.get_sync_state()
    assert state == {"history_id": "42", "last_full_sync": 999}
    # Overwrite a subset.
    store.set_sync_state(history_id="43", last_full_sync=1000)
    assert store.get_sync_state()["history_id"] == "43"


def test_migration_drops_unused_fts_index(store):
    """The v4 migration removes the never-queried FTS5 table and its triggers."""
    conn = store.connect()
    objects = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE name = 'messages_fts' OR name LIKE 'messages_a%'"
        )
    }
    assert objects == set()


# -- triage / executor support ----------------------------------------------


def test_messages_in_group_orders_newest_first(store):
    store.upsert_messages(
        [
            _msg("m1", group_key="g", date=100),
            _msg("m2", group_key="g", date=300),
            _msg("m3", group_key="other", date=200),
        ]
    )
    rows = store.messages_in_group("g")
    assert [r["id"] for r in rows] == ["m2", "m1"]
    assert rows[0]["label_ids"] == ["INBOX", "UNREAD"]  # decoded back to a list


def test_messages_in_group_inbox_only(store):
    store.upsert_messages(
        [
            _msg("m1", group_key="g", label_ids=["INBOX"]),
            _msg("m2", group_key="g", label_ids=["UNREAD"]),  # archived
        ]
    )
    assert {r["id"] for r in store.messages_in_group("g", inbox_only=True)} == {"m1"}


def test_message_ids_in_group(store):
    store.upsert_messages(
        [_msg("m1", group_key="g"), _msg("m2", group_key="g"), _msg("m3", group_key="h")]
    )
    assert sorted(store.message_ids_in_group("g")) == ["m1", "m2"]


def test_group_sender_single_vs_many(store):
    store.upsert_messages(
        [
            _msg("m1", group_key="g", from_addr="a@x.com"),
            _msg("m2", group_key="g", from_addr="a@x.com"),
            _msg("m3", group_key="list", from_addr="a@x.com"),
            _msg("m4", group_key="list", from_addr="b@x.com"),
        ]
    )
    assert store.group_sender("g") == "a@x.com"
    assert store.group_sender("list") is None  # spans senders -> no single from


def test_actions_log_roundtrip_and_delete(store):
    aid = store.log_action(
        target="news@x.com",
        action="archive",
        prior_state={"ids": ["m1"], "undo_add": ["INBOX"], "undo_remove": []},
    )
    rows = store.recent_actions()
    assert len(rows) == 1
    assert rows[0]["id"] == aid
    assert rows[0]["target"] == "news@x.com"
    assert rows[0]["prior_state"]["ids"] == ["m1"]

    store.update_action_state(aid, {"ids": ["m1"], "filter_id": "f1"})
    assert store.recent_actions()[0]["prior_state"]["filter_id"] == "f1"

    store.delete_action(aid)
    assert store.recent_actions() == []


def test_recent_actions_newest_first(store):
    a1 = store.log_action(target="a", action="archive", prior_state={})
    a2 = store.log_action(target="b", action="trash", prior_state={})
    assert [r["id"] for r in store.recent_actions()] == [a2, a1]

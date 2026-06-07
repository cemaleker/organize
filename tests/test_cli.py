"""Tests for the headless (--no-ui) stdout reporter."""

import io

from inbox_cleaner.cli import _StdoutReporter
from inbox_cleaner.events import (
    STATE_DONE,
    STATE_FETCHING,
    STATE_RATE_LIMITED,
    GroupSnapshot,
    SyncDone,
    SyncProgress,
)


def _run(events):
    out = io.StringIO()
    reporter = _StdoutReporter(out)
    for e in events:
        reporter(e)
    return out.getvalue()


def test_progress_and_snapshots_stay_on_one_line():
    # A whole run's worth of progress + snapshot events must not emit a single
    # newline — everything is rewritten in place with carriage returns.
    text = _run(
        [
            SyncProgress(0, 100, STATE_FETCHING),
            SyncProgress(50, 100, STATE_FETCHING),
            GroupSnapshot(rows=[("a",), ("b",)]),
            SyncProgress(100, 100, STATE_FETCHING),
            GroupSnapshot(rows=[("a",), ("b",), ("c",)]),
        ]
    )
    assert "\n" not in text
    assert "\r" in text


def test_snapshot_folds_group_count_into_status_line():
    text = _run(
        [
            SyncProgress(50, 100, STATE_FETCHING),
            GroupSnapshot(rows=[("a",), ("b",), ("c",)]),
        ]
    )
    # The latest rendered segment carries both the counter and the group count.
    last = text.split("\r")[-1]
    assert "50/100" in last
    assert "3 groups" in last


def test_rate_limit_notice_folds_into_the_line():
    text = _run([SyncProgress(10, 100, STATE_RATE_LIMITED, retry_after=4.0)])
    assert "\n" not in text
    assert "rate limited" in text
    assert "4s" in text


def test_done_commits_a_single_final_line():
    text = _run(
        [
            SyncProgress(100, 100, STATE_FETCHING),
            SyncDone(fetched=100, errors=0),
            # SyncProgress with STATE_DONE may also arrive; it shouldn't add lines.
        ]
    )
    assert text.count("\n") == 1
    assert text.rstrip().endswith("Done. Fetched 100.")


def test_done_summary_includes_extras_and_inline_error_causes():
    text = _run(
        [
            SyncDone(
                fetched=98,
                errors=2,
                skipped=10,
                removed=3,
                error_reasons={"404 notFound": 2},
            )
        ]
    )
    assert text.count("\n") == 1
    line = text.strip()
    assert "98" in line
    assert "10 already indexed" in line
    assert "3 removed" in line
    assert "2 errors: 404 notFound×2" in line


def test_done_clears_a_longer_live_line():
    # A long live line followed by a short summary must not leave stale chars.
    text = _run(
        [
            SyncProgress(123456, 999999, STATE_RATE_LIMITED, retry_after=60.0),
            SyncDone(fetched=1, errors=0),
        ]
    )
    final = text.split("\r")[-1]
    # Everything after the summary text is padding spaces then the newline.
    assert final.startswith("Done. Fetched 1.")
    assert final.endswith("\n")
    assert final.strip() == "Done. Fetched 1."


def test_cancelled_summary_uses_cancelled_verb():
    text = _run([SyncDone(fetched=5, errors=0, cancelled=True)])
    assert "Cancelled. Fetched 5." in text


def test_done_after_progress_done_state_is_idempotent_lines():
    text = _run(
        [
            SyncProgress(100, 100, STATE_FETCHING),
            SyncProgress(100, 100, STATE_DONE),
            SyncDone(fetched=100, errors=0),
        ]
    )
    # Only the SyncDone commits a newline; the STATE_DONE progress is in place.
    assert text.count("\n") == 1


# -- undo command -----------------------------------------------------------


def test_undo_nothing_logged(tmp_path, capsys):
    from inbox_cleaner.cli import main

    rc = main(["--db", str(tmp_path / "i.sqlite"), "undo"])
    assert rc == 0
    assert "Nothing to undo" in capsys.readouterr().out


def test_undo_list_shows_logged_actions(tmp_path, capsys):
    from inbox_cleaner.cli import main
    from inbox_cleaner.store import Store

    db = tmp_path / "i.sqlite"
    store = Store(db)
    store.connect()
    store.log_action(
        target="news@x.com",
        action="archive",
        prior_state={"ids": ["m1", "m2"], "filter_id": "f1"},
    )
    store.close()

    rc = main(["--db", str(db), "undo", "--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "news@x.com" in out
    assert "archive" in out
    assert "+filter" in out

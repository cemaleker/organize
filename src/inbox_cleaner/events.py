"""Sync engine events.

Deliberately **plain dataclasses with no Textual dependency**. The engine emits
these via its ``emit`` callback, so it can run fully headless (the stdout
consumer in ``cli.py``, or a test) and stay unit-testable. UI consumers (the
triage TUI) adapt these onto the event loop themselves; the engine never imports
Textual.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Progress states, surfaced so a UI can render distinct affordances (e.g. a
# countdown when rate-limited).
STATE_LISTING = "listing"
STATE_FETCHING = "fetching"
STATE_WRITING = "writing"
STATE_RATE_LIMITED = "rate_limited"
STATE_DONE = "done"
STATE_CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SyncProgress:
    """Emitted as the backfill streams through the mailbox."""

    fetched: int
    total: int | None
    state: str
    retry_after: float | None = None


@dataclass(frozen=True, slots=True)
class GroupSnapshot:
    """A periodic snapshot of the grouped counts, for a live table.

    Each row is (category, group_key, total, unread, oldest, newest).
    """

    rows: list[tuple] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SyncDone:
    """Emitted once when the sync finishes, is cancelled, or exhausts the mailbox.

    fetched: messages fetched from Gmail and upserted.
    skipped: ids already in the local index that were not re-fetched.
    removed: local rows deleted (trashed/spam, permanently deleted, or pruned
        because they no longer exist remotely).
    error_reasons: per-message fetch failures broken down by cause
        ("<status> <reason>" -> count); the values sum to ``errors``.
    """

    fetched: int
    errors: int
    cancelled: bool = False
    skipped: int = 0
    removed: int = 0
    error_reasons: dict[str, int] = field(default_factory=dict)

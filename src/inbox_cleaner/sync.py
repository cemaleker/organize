"""Headless sync engine — the backfill side of the pipeline.

Phase 3 of the plan. Knows nothing about Textual: it takes a Gmail ``service``,
a :class:`~inbox_cleaner.store.Store`, and an ``emit(event)`` callback, then
streams the whole mailbox into the local index.

Flow (``backfill``):

1. ``getProfile`` (best effort) for a progress total + the current ``historyId``
   (saved at the end for Phase 6's incremental sync).
2. For each page: list ids -> batch ``format=metadata`` fetch -> parse headers
   -> classify -> ``upsert_messages``.
3. Emit :class:`SyncProgress` per batch and a :class:`GroupSnapshot` every
   ``snapshot_every`` messages.
4. On completion: store ``history_id`` + ``last_full_sync``, emit
   :class:`SyncDone`.

A backfill always starts from the first page; it is not resumable mid-run.
Already-indexed ids are smart-skipped (unless ``full``), so re-running after an
interruption re-lists from the start but only re-fetches what is missing.

Rate limits (429 / 403 rateLimitExceeded / 5xx) are retried with exponential
backoff via tenacity; a ``rate_limited`` progress event is emitted before each
backoff sleep. This applies at two levels: a whole-batch HTTP failure is retried
by tenacity, and an individual message that gets throttled *inside* a batch is
re-fetched in-place (see :meth:`SyncEngine._fetch_chunk`) so it recovers during
the run instead of surfacing as an error at the end. ``sleep`` is injectable so
tests don't actually wait.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from collections.abc import Callable
from typing import Any, cast

from googleapiclient.errors import HttpError
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .classify import (
    classify,
    classify_headers_subset,
    headers_to_dict,
    sender_address,
    sender_domain,
)
from .events import (
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FETCHING,
    STATE_LISTING,
    STATE_RATE_LIMITED,
    GroupSnapshot,
    SyncDone,
    SyncProgress,
)
from .store import Store

# History change types we care about for the incremental delta.
_HISTORY_TYPES = ("messageAdded", "messageDeleted", "labelAdded", "labelRemoved")

# Gmail caps a single batch HTTP request at 1000 sub-requests. We chunk by
# batch_size, but never exceed this hard limit regardless of config.
_MAX_BATCH = 1000

# Labels that mean a message should not be in the index. messages.list excludes
# these by default, but a message trashed *after* it was indexed still needs to
# be removed locally — so we drop any fetched message carrying one of these.
_EXCLUDED_LABELS = frozenset({"TRASH", "SPAM"})

# HTTP statuses that warrant a retry. 403 is only retried when the error reason
# is a rate-limit one (checked separately) — a plain 403 is a real auth/scope
# problem and must surface immediately.
_RETRY_STATUSES = frozenset({429, 500, 502, 503})
_RATE_LIMIT_REASONS = ("ratelimitexceeded", "userratelimitexceeded")


def _is_rate_limit(exc: BaseException) -> bool:
    """True if the exception is a transient Gmail rate-limit / server error."""
    if not isinstance(exc, HttpError):
        return False
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in _RETRY_STATUSES:
        return True
    if status == 403:
        text = str(exc).lower()
        return any(reason in text for reason in _RATE_LIMIT_REASONS)
    return False


def _error_reason(exc: BaseException) -> str:
    """A short, groupable label for a failed fetch — ``"<status> <reason>"``.

    Gmail returns a JSON body with ``error.errors[0].reason`` (e.g.
    ``rateLimitExceeded``, ``notFound``); we pair it with the HTTP status so the
    sync summary can break errors down by cause instead of dropping them.
    """
    if not isinstance(exc, HttpError):
        return type(exc).__name__
    status = getattr(getattr(exc, "resp", None), "status", None)
    reason: str | None = None
    try:
        content = exc.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        error = json.loads(content).get("error", {})
        errors = error.get("errors") or []
        reason = (errors[0].get("reason") if errors else None) or error.get("status")
    except (ValueError, AttributeError, TypeError):
        pass
    if status and reason:
        return f"{status} {reason}"
    return str(status) if status else (reason or "HttpError")


class SyncEngine:
    """Backfills the whole mailbox into a :class:`Store`, emitting events."""

    def __init__(
        self,
        service: Any,
        store: Store,
        emit: Callable[[object], None] | None = None,
        *,
        query: str | None = None,
        batch_size: int = 100,
        snapshot_every: int = 500,
        max_attempts: int = 7,
        sleep: Callable[[float], None] = time.sleep,
        cancel_event: threading.Event | None = None,
        full: bool = False,
    ) -> None:
        self.service = service
        self.store = store
        self.emit = emit or (lambda _event: None)
        self.query = query
        self.batch_size = batch_size
        self.snapshot_every = snapshot_every
        self.max_attempts = max_attempts
        # Set from another thread to request a graceful stop. The loop checks it
        # between batches and returns promptly; already-fetched messages stay in
        # the index, so a re-run smart-skips them.
        self.cancel_event = cancel_event
        # When a cancel_event is present, use it as the backoff sleep so a
        # rate-limit wait is interrupted immediately on cancel (Event.wait
        # returns the moment the event is set) instead of blocking up to 60s.
        if cancel_event is not None and sleep is time.sleep:
            self._sleep: Callable[[float], object] = cancel_event.wait
        else:
            self._sleep = sleep
        # Re-fetch everything even if already indexed (default: smart-skip).
        self.full = full

        self.fetched = 0
        self.skipped = 0
        self.removed = 0
        self.errors = 0
        # Per-message fetch failures broken down by cause (status + reason), so
        # a large error count can be diagnosed instead of guessed at.
        self.error_reasons: Counter[str] = Counter()
        self.total: int | None = None
        self._history_id: str | None = None
        self._snapshots_emitted = 0

    def _cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    @property
    def processed(self) -> int:
        """Messages scanned so far (fetched + skipped) — drives the progress bar."""
        return self.fetched + self.skipped

    def _emit_progress(
        self, state: str, *, total: int | None = None, retry_after: float | None = None
    ) -> None:
        self.emit(
            SyncProgress(
                self.processed,
                self.total if total is None else total,
                state,
                retry_after=retry_after,
            )
        )

    def _apply_messages(self, messages: list[dict[str, Any]]) -> tuple[int, int]:
        """Upsert indexable messages; delete any now carrying TRASH/SPAM.

        Returns ``(upserted, removed)``.
        """
        rows: list[dict[str, Any]] = []
        trashed: list[str] = []
        for msg in messages:
            labels = msg.get("labelIds") or []
            if any(label in _EXCLUDED_LABELS for label in labels):
                trashed.append(msg["id"])
            else:
                rows.append(self._to_row(msg))
        if rows:
            self.store.upsert_messages(rows)
        removed = self.store.delete_messages(trashed) if trashed else 0
        return len(rows), removed

    # -- retry plumbing ------------------------------------------------------

    def _execute(self, fn: Callable[[], Any]) -> Any:
        """Run a network call with rate-limit-aware exponential backoff."""
        retrying = Retrying(
            retry=retry_if_exception(_is_rate_limit),
            wait=wait_exponential(multiplier=1, max=60),
            stop=stop_after_attempt(self.max_attempts),
            # _sleep may be Event.wait (returns bool); tenacity ignores the
            # return, so the wider Callable is safe to pass as its sleep hook.
            sleep=cast("Callable[[float], None]", self._sleep),
            before_sleep=self._on_backoff,
            reraise=True,
        )
        return retrying(fn)

    def _on_backoff(self, retry_state: Any) -> None:
        delay = getattr(retry_state.next_action, "sleep", None)
        self._emit_progress(STATE_RATE_LIMITED, retry_after=delay)

    # -- public API ----------------------------------------------------------

    def backfill(self) -> SyncDone:
        """Backfill the mailbox from the first page.

        Smart by default: ids already in the index are not re-fetched (pass
        ``full=True`` to force). When doing a complete unfiltered pass (no
        query), records every listed id and prunes local rows that are no longer
        present remotely — so mail trashed/deleted since the last sync is cleaned
        up.
        """
        self._load_total_and_history()

        token: str | None = None

        # Prune only makes sense for a complete, unfiltered pass: a query is a
        # partial view, so its listed ids can't tell us what's been removed.
        seen: set[str] | None = set() if self.query is None else None

        while True:
            if self._cancelled():
                return self._finish(cancelled=True)
            self._emit_progress(STATE_LISTING)
            page = self._list_page(token)
            ids = [m["id"] for m in page.get("messages", [])]
            if seen is not None:
                seen.update(ids)

            to_fetch = ids
            if ids and not self.full:
                have = self.store.existing_ids(ids)
                to_fetch = [i for i in ids if i not in have]
                self.skipped += len(ids) - len(to_fetch)

            if to_fetch:
                self._emit_progress(STATE_FETCHING)
                messages, batch_errors = self._fetch_metadata(to_fetch)
                self.errors += batch_errors
                upserted, removed = self._apply_messages(messages)
                self.fetched += upserted
                self.removed += removed

            token = page.get("nextPageToken")

            self._emit_progress(STATE_FETCHING)
            self._maybe_snapshot()

            if not token:
                break

        if seen is not None:
            self.removed += self.store.prune_missing(seen)

        return self._finish()

    def incremental(self) -> SyncDone:
        """Apply History API deltas since the last sync.

        Falls back to a full :meth:`backfill` when there is no stored
        ``history_id`` (never synced) or when Gmail returns ``404`` because the
        stored id is older than its ~1 week of retained history.
        """
        self._load_total_and_history()
        start = (self.store.get_sync_state() or {}).get("history_id")
        if not start:
            return self.backfill()

        try:
            changed, deleted, new_history_id = self._collect_history(start)
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                # History expired — only a full resync can recover.
                return self.backfill()
            raise

        # A message added then deleted within the window is just gone.
        changed -= deleted

        # Fetch + upsert changed ids in chunks, so a large delta (e.g. a stale
        # history id that replays most of the mailbox) streams with progress
        # and stays cancellable, rather than building one oversized batch.
        changed_list = list(changed)
        total = len(changed_list)
        for start in range(0, total, self._chunk_size):
            if self._cancelled():
                return self._finish(cancelled=True)
            chunk = changed_list[start : start + self._chunk_size]
            self._emit_progress(STATE_FETCHING, total=total)
            messages, batch_errors = self._fetch_chunk(chunk)
            self.errors += batch_errors
            # Upsert live mail; drop any that has since been trashed/spammed.
            upserted, removed = self._apply_messages(messages)
            self.fetched += upserted
            self.removed += removed
            self._maybe_snapshot()

        if deleted:
            self.removed += self.store.delete_messages(deleted)

        if new_history_id:
            self.store.set_sync_state(history_id=new_history_id)

        return self._finish()

    def _collect_history(self, start: str) -> tuple[set[str], set[str], str]:
        """Page through history records, returning (changed, deleted, new_id)."""
        changed: set[str] = set()
        deleted: set[str] = set()
        new_history_id = start
        token: str | None = None

        while True:
            self._emit_progress(STATE_LISTING)
            resp = self._list_history(start, token)
            new_history_id = resp.get("historyId", new_history_id)
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    changed.add(added["message"]["id"])
                for labeled in record.get("labelsAdded", []):
                    changed.add(labeled["message"]["id"])
                for labeled in record.get("labelsRemoved", []):
                    changed.add(labeled["message"]["id"])
                for removed in record.get("messagesDeleted", []):
                    deleted.add(removed["message"]["id"])
            token = resp.get("nextPageToken")
            if not token:
                break

        return changed, deleted, new_history_id

    def _list_history(self, start: str, token: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "userId": "me",
            "startHistoryId": start,
            "historyTypes": list(_HISTORY_TYPES),
        }
        if token:
            kwargs["pageToken"] = token
        req = self.service.users().history().list(**kwargs)
        return self._execute(req.execute)

    # -- steps ---------------------------------------------------------------

    def _load_total_and_history(self) -> None:
        """Best-effort profile fetch for the progress total + history id."""
        try:
            profile = self._execute(
                self.service.users().getProfile(userId="me").execute
            )
        except HttpError:
            return
        self.total = profile.get("messagesTotal")
        self._history_id = profile.get("historyId")

    def _list_page(self, token: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"userId": "me", "maxResults": self.batch_size}
        if token:
            kwargs["pageToken"] = token
        if self.query:
            kwargs["q"] = self.query
        req = self.service.users().messages().list(**kwargs)
        return self._execute(req.execute)

    @property
    def _chunk_size(self) -> int:
        return min(self.batch_size, _MAX_BATCH)

    def _fetch_metadata(self, ids: list[str]) -> tuple[list[dict[str, Any]], int]:
        """Batch-fetch ``format=metadata`` for ids, chunked to the batch limit.

        Returns ``(messages, error_count)``. ids longer than the chunk size are
        split across multiple batch requests (Gmail caps a batch at 1000).
        """
        messages: list[dict[str, Any]] = []
        errors = 0
        for start in range(0, len(ids), self._chunk_size):
            chunk_msgs, chunk_errs = self._fetch_chunk(ids[start : start + self._chunk_size])
            messages.extend(chunk_msgs)
            errors += chunk_errs
        return messages, errors

    def _fetch_chunk(self, ids: list[str]) -> tuple[list[dict[str, Any]], int]:
        """Fetch one batch (<= chunk size) of message metadata.

        Per-message failures don't abort the run. Transient ones (rate limit /
        5xx) are re-fetched in-place with exponential backoff, so a member that
        gets throttled inside an otherwise-fine batch recovers during the run.
        Permanent ones (e.g. ``404 notFound``) are counted immediately — a retry
        would never succeed. Whatever is still throttled after ``max_attempts``
        is counted too. The batch HTTP call itself is also retried on rate
        limits (via :meth:`_execute`).

        Returns ``(messages, error_count)``.
        """
        messages: list[dict[str, Any]] = []
        errors = 0
        pending = list(ids)
        attempt = 0

        while pending:
            attempt += 1
            batch_msgs, failures = self._run_batch(pending)
            messages.extend(batch_msgs)

            retry = [mid for mid, exc in failures.items() if _is_rate_limit(exc)]
            for mid, exc in failures.items():
                if not _is_rate_limit(exc):
                    errors += 1
                    self.error_reasons[_error_reason(exc)] += 1

            # Give up on the still-throttled members once we're out of attempts
            # or a cancel has been requested; count them as errors.
            if not retry or attempt >= self.max_attempts or self._cancelled():
                for mid in retry:
                    errors += 1
                    self.error_reasons[_error_reason(failures[mid])] += 1
                break

            delay = min(2.0 ** (attempt - 1), 60.0)
            self._emit_progress(STATE_RATE_LIMITED, retry_after=delay)
            self._sleep(delay)
            pending = retry

        return messages, errors

    def _run_batch(
        self, ids: list[str]
    ) -> tuple[list[dict[str, Any]], dict[str, BaseException]]:
        """Execute one batch HTTP request for ``ids``.

        Returns ``(messages, failures)`` where ``failures`` maps a message id to
        the exception its sub-request raised. The batch call itself is retried on
        a whole-request rate limit via :meth:`_execute`.
        """
        messages: list[dict[str, Any]] = []
        failures: dict[str, BaseException] = {}

        def _callback(request_id: str, response: Any, exception: Exception | None):
            if exception is not None:
                failures[request_id] = exception
            elif response is not None:
                messages.append(response)

        def _run() -> None:
            batch = self.service.new_batch_http_request()
            for mid in ids:
                req = (
                    self.service.users()
                    .messages()
                    .get(userId="me", id=mid, format="metadata")
                )
                batch.add(req, callback=_callback, request_id=mid)
            batch.execute()

        self._execute(_run)
        return messages, failures

    def _to_row(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Map a Gmail metadata message resource to a store row."""
        headers = headers_to_dict(msg.get("payload", {}).get("headers", []))
        category, group_key = classify(headers)
        label_ids = msg.get("labelIds") or []
        internal = msg.get("internalDate")
        return {
            "id": msg["id"],
            "thread_id": msg.get("threadId"),
            "from_addr": sender_address(headers),
            "from_domain": sender_domain(headers),
            "subject": headers.get("Subject"),
            "date": int(internal) if internal else None,
            "size_estimate": msg.get("sizeEstimate"),
            "is_unread": "UNREAD" in label_ids,
            "label_ids": label_ids,
            "snippet": msg.get("snippet"),
            "category": category,
            "group_key": group_key,
            "class_headers": classify_headers_subset(headers),
        }

    def _maybe_snapshot(self) -> None:
        """Emit a GroupSnapshot when we've crossed another snapshot threshold."""
        due = self.fetched // self.snapshot_every
        if due > self._snapshots_emitted:
            self._snapshots_emitted = due
            self.emit(GroupSnapshot(rows=self.store.group_counts()))

    def _finish(self, cancelled: bool = False) -> SyncDone:
        """Emit terminal events. On normal completion, advance the sync cursor;
        on cancel, persist nothing (the run was incomplete)."""
        if not cancelled:
            self.store.set_sync_state(
                history_id=self._history_id,
                last_full_sync=int(time.time() * 1000),
            )
        # A final snapshot so consumers see the complete picture.
        self.emit(GroupSnapshot(rows=self.store.group_counts()))
        state = STATE_CANCELLED if cancelled else STATE_DONE
        self._emit_progress(state)
        done = SyncDone(
            fetched=self.fetched,
            errors=self.errors,
            cancelled=cancelled,
            skipped=self.skipped,
            removed=self.removed,
            error_reasons=dict(self.error_reasons),
        )
        self.emit(done)
        return done

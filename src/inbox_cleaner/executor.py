"""Executor — the write path. The *only* component that mutates Gmail.

Phase 7 of the plan. It takes an authorized **write** service (``gmail.modify``
+ ``gmail.settings.basic`` — see :func:`inbox_cleaner.auth.get_write_service`)
and a :class:`~inbox_cleaner.store.Store`, and turns a per-sender triage decision
into two Gmail calls:

- **Backlog** — ``messages.batchModify`` over every indexed id in the group
  (chunked to Gmail's 1000-id limit). ``trash`` adds the ``TRASH`` label,
  ``archive`` removes ``INBOX``, ``label`` adds a label and removes ``INBOX``.
- **Future** — ``settings.filters.create``, so the same sender (or mailing list)
  is auto-handled from now on. Gmail enforces the filter; nothing syncs back.

Safety model (see docs/DESIGN.md):

- Only reversible operations: label / archive / trash. No permanent delete (the
  scope can't, by design).
- Every action is written to ``actions_log`` *before* it runs, recording the
  affected ids, the **inverse** label delta, and any created filter id — so
  :meth:`Executor.undo` can walk the log backward and put everything back.

Like the sync engine, this is headless and testable: it knows nothing about the
TUI and takes a plain ``service``. Rate limits are retried with exponential
backoff.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from googleapiclient.errors import HttpError
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .store import Store

# Gmail caps messages.batchModify at 1000 ids per request.
_MAX_MODIFY = 1000

# Valid triage actions. "keep" is a no-op the caller filters out before applying.
ACTIONS = ("trash", "archive", "label", "keep")

# HTTP statuses worth retrying; 403 only when it's a rate-limit reason.
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
        return any(reason in str(exc).lower() for reason in _RATE_LIMIT_REASONS)
    return False


@dataclass(slots=True)
class Action:
    """A triage decision for one sender group.

    Attributes:
        group_key: the index ``group_key`` whose messages to act on.
        category: the group's category (drives the filter criterion — a
            ``mailing_list`` matches on its list id, everything else on ``from:``).
        kind: ``trash`` | ``archive`` | ``label``.
        target: human-facing label for the action (sender address or list id),
            recorded in the log; defaults to ``group_key``.
        label_name: required when ``kind == "label"`` — the Gmail label to apply
            (created if it does not exist).
        create_filter: also create a Gmail filter so future mail is auto-handled.
    """

    group_key: str
    category: str = "personal"
    kind: str = "archive"
    target: str | None = None
    label_name: str | None = None
    create_filter: bool = True

    def __post_init__(self) -> None:
        if self.kind not in ("trash", "archive", "label"):
            raise ValueError(f"unsupported action kind: {self.kind!r}")
        if self.kind == "label" and not self.label_name:
            raise ValueError("a 'label' action requires label_name")
        if self.target is None:
            self.target = self.group_key


@dataclass(slots=True)
class ActionResult:
    """Outcome of applying one :class:`Action`."""

    target: str
    kind: str
    matched: int = 0          # ids found in the group
    modified: int = 0         # ids sent to batchModify
    filter_id: str | None = None
    error: str | None = None


@dataclass(slots=True)
class ApplyResult:
    """Aggregate outcome of an :meth:`Executor.apply_all` pass."""

    results: list[ActionResult] = field(default_factory=list)

    @property
    def modified(self) -> int:
        return sum(r.modified for r in self.results)

    @property
    def filters_created(self) -> int:
        return sum(1 for r in self.results if r.filter_id)

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.error)


# Forward label delta per kind: (addLabelIds, removeLabelIds). label_id is spliced
# in for the "label" kind. Trash adds TRASH (Gmail moves it out of the inbox);
# archive just drops INBOX.
def _forward_delta(kind: str, label_id: str | None) -> tuple[list[str], list[str]]:
    if kind == "trash":
        return ["TRASH"], []
    if kind == "archive":
        return [], ["INBOX"]
    # label
    return [cast(str, label_id)], ["INBOX"]


# Inverse delta used by undo: what to add / remove to put a message back.
def _inverse_delta(kind: str, label_id: str | None) -> tuple[list[str], list[str]]:
    if kind == "trash":
        return ["INBOX"], ["TRASH"]      # un-trash and restore to inbox
    if kind == "archive":
        return ["INBOX"], []
    # label: restore to inbox and strip the label we added
    return ["INBOX"], [cast(str, label_id)]


class Executor:
    """Applies triage actions to Gmail, reversibly and idempotently."""

    def __init__(
        self,
        service: Any,
        store: Store,
        *,
        max_attempts: int = 7,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self.service = service
        self.store = store
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._label_cache: dict[str, str] | None = None

    # -- retry plumbing ------------------------------------------------------

    def _execute(self, fn: Callable[[], Any]) -> Any:
        """Run a network call with rate-limit-aware exponential backoff."""
        kwargs: dict[str, Any] = {
            "retry": retry_if_exception(_is_rate_limit),
            "wait": wait_exponential(multiplier=1, max=60),
            "stop": stop_after_attempt(self.max_attempts),
            "reraise": True,
        }
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        return Retrying(**kwargs)(fn)

    # -- labels --------------------------------------------------------------

    def _labels(self) -> dict[str, str]:
        """Return a cached ``name -> id`` map of the user's Gmail labels."""
        if self._label_cache is None:
            resp = self._execute(
                self.service.users().labels().list(userId="me").execute
            )
            self._label_cache = {
                lbl["name"]: lbl["id"] for lbl in resp.get("labels", [])
            }
        return self._label_cache

    def resolve_label(self, name: str) -> str:
        """Return the id of label ``name``, creating it if it does not exist."""
        labels = self._labels()
        if name in labels:
            return labels[name]
        created = self._execute(
            self.service.users()
            .labels()
            .create(userId="me", body={"name": name})
            .execute
        )
        labels[name] = created["id"]
        return created["id"]

    # -- applying actions ----------------------------------------------------

    def apply(self, action: Action) -> ActionResult:
        """Apply one action: edit the backlog, optionally create a filter.

        Logs the action (with its inverse) to ``actions_log`` before touching
        Gmail, so a partial failure is still undoable.
        """
        target = cast(str, action.target)
        result = ActionResult(target=target, kind=action.kind)
        label_id = (
            self.resolve_label(action.label_name)  # type: ignore[arg-type]
            if action.kind == "label"
            else None
        )

        ids = self.store.message_ids_in_group(action.group_key)
        result.matched = len(ids)
        add, remove = _forward_delta(action.kind, label_id)
        undo_add, undo_remove = _inverse_delta(action.kind, label_id)

        # Log BEFORE acting. filter_id is filled in after the create returns.
        prior_state: dict[str, Any] = {
            "ids": ids,
            "undo_add": undo_add,
            "undo_remove": undo_remove,
            "label_id": label_id,
            "filter_id": None,
        }
        action_id = self.store.log_action(
            target=target, action=action.kind, prior_state=prior_state
        )

        try:
            result.modified = self._modify(ids, add, remove)
            if action.create_filter:
                result.filter_id = self._create_filter(action, label_id)
                if result.filter_id:
                    prior_state["filter_id"] = result.filter_id
                    self.store.update_action_state(action_id, prior_state)
        except HttpError as exc:
            result.error = _describe(exc)
        return result

    def apply_all(self, actions: list[Action]) -> ApplyResult:
        """Apply a batch of actions, returning the aggregate result."""
        return ApplyResult(results=[self.apply(a) for a in actions])

    def _modify(self, ids: list[str], add: list[str], remove: list[str]) -> int:
        """batchModify ``ids`` in chunks; returns the number sent. No-op if the
        delta is empty (e.g. a filter-only future action with no backlog)."""
        if not ids or (not add and not remove):
            return 0
        body: dict[str, Any] = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        sent = 0
        for start in range(0, len(ids), _MAX_MODIFY):
            chunk = ids[start : start + _MAX_MODIFY]
            self._execute(
                self.service.users()
                .messages()
                .batchModify(userId="me", body={**body, "ids": chunk})
                .execute
            )
            sent += len(chunk)
        return sent

    def _create_filter(self, action: Action, label_id: str | None) -> str | None:
        """Create a Gmail filter mirroring the action for future mail.

        Mailing-list groups match on the list id (``list:...``); everything else
        matches the group's sender address. Returns the new filter id, or None
        if no usable criterion could be derived.
        """
        criteria = self._filter_criteria(action)
        if criteria is None:
            return None
        add, remove = _forward_delta(action.kind, label_id)
        filter_action: dict[str, Any] = {}
        if add:
            filter_action["addLabelIds"] = add
        if remove:
            filter_action["removeLabelIds"] = remove
        created = self._execute(
            self.service.users()
            .settings()
            .filters()
            .create(userId="me", body={"criteria": criteria, "action": filter_action})
            .execute
        )
        return created.get("id")

    def _filter_criteria(self, action: Action) -> dict[str, str] | None:
        if action.category == "mailing_list":
            return {"query": f"list:{action.group_key}"}
        sender = self.store.group_sender(action.group_key) or action.group_key
        return {"from": sender} if sender else None

    # -- undo ----------------------------------------------------------------

    def undo(self, logged: dict[str, Any]) -> ActionResult:
        """Reverse a single logged action (a row from ``recent_actions``).

        Applies the stored inverse label delta to the affected ids and deletes
        the created filter, then removes the log row.
        """
        state = logged.get("prior_state", {})
        target = logged.get("target", "?")
        result = ActionResult(target=target, kind=f"undo:{logged.get('action')}")
        ids = state.get("ids", [])
        try:
            result.modified = self._modify(
                ids, state.get("undo_add", []), state.get("undo_remove", [])
            )
            filter_id = state.get("filter_id")
            if filter_id:
                self._delete_filter(filter_id)
            self.store.delete_action(int(logged["id"]))
        except HttpError as exc:
            result.error = _describe(exc)
        return result

    def undo_last(self, n: int = 1) -> ApplyResult:
        """Undo the ``n`` most recent logged actions, newest first."""
        rows = self.store.recent_actions(limit=n)
        return ApplyResult(results=[self.undo(r) for r in rows])

    def _delete_filter(self, filter_id: str) -> None:
        self._execute(
            self.service.users()
            .settings()
            .filters()
            .delete(userId="me", id=filter_id)
            .execute
        )


def _describe(exc: HttpError) -> str:
    """Short, human-readable description of a failed Gmail call."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    return f"{status} {exc}" if status else str(exc)

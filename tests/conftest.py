"""Shared test fakes — a minimal in-memory stand-in for the Gmail service.

Faithful to the slices of the API the sync engine actually uses:
``users().getProfile``, ``users().messages().list``,
``users().messages().get`` (driven through batch), and
``new_batch_http_request``. It lets the engine be tested fully headless, per the
plan's "test the engine against fixtures" rule.
"""

from __future__ import annotations

import json

from googleapiclient.errors import HttpError


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"


def http_error(status: int, reason: str = "") -> HttpError:
    content = json.dumps(
        {"error": {"message": reason, "errors": [{"reason": reason}]}}
    ).encode()
    return HttpError(_Resp(status), content)


def make_message(
    msg_id: str,
    *,
    from_="alice@example.com",
    subject="Hello",
    labels=("INBOX", "UNREAD"),
    date_ms=1000,
    extra_headers=None,
):
    """Build a Gmail metadata message resource fixture."""
    headers = [{"name": "From", "value": from_}, {"name": "Subject", "value": subject}]
    for name, value in (extra_headers or {}).items():
        headers.append({"name": name, "value": value})
    return {
        "id": msg_id,
        "threadId": f"t-{msg_id}",
        "labelIds": list(labels),
        "internalDate": str(date_ms),
        "sizeEstimate": 4096,
        "snippet": f"snippet for {msg_id}",
        "payload": {"headers": headers},
    }


class _ListRequest:
    def __init__(self, service: "FakeService", kwargs: dict) -> None:
        self._service = service
        self._kwargs = kwargs

    def execute(self):
        token = self._kwargs.get("pageToken")
        if token in self._service.fail_list_tokens:
            raise http_error(429, "rateLimitExceeded")
        if self._service.list_failures > 0:
            self._service.list_failures -= 1
            raise http_error(429, "rateLimitExceeded")
        return self._service.page_for(token)


class _GetRequest:
    def __init__(self, service: "FakeService", msg_id: str) -> None:
        self._service = service
        self.msg_id = msg_id

    @property
    def message(self):
        return self._service.messages_by_id.get(self.msg_id)


class _SimpleRequest:
    def __init__(self, service: "FakeService", payload, record=None) -> None:
        self._service = service
        self._payload = payload
        self._record = record

    def execute(self):
        if self._service.profile_failures > 0:
            self._service.profile_failures -= 1
            raise http_error(429, "rateLimitExceeded")
        # Generic transient-failure injection keyed by the recorded call kind
        # (e.g. "modify"), so executor backoff can be exercised.
        if self._record is not None:
            kind = self._record[0]
            if self._service.call_failures.get(kind, 0) > 0:
                self._service.call_failures[kind] -= 1
                raise http_error(429, "rateLimitExceeded")
            self._service.calls.append(self._record)
        return self._payload


class _Batch:
    def __init__(self, service: "FakeService") -> None:
        self._service = service
        self._items: list = []

    def add(self, request, callback, request_id=None):
        self._items.append((request, callback, request_id))

    def execute(self):
        self._service.batch_sizes.append(len(self._items))
        if self._service.batch_failures > 0:
            self._service.batch_failures -= 1
            raise http_error(429, "rateLimitExceeded")
        member_failures = self._service.member_failures
        for req, cb, request_id in self._items:
            rid = request_id if request_id is not None else req.msg_id
            # A member throttled a configured number of times before recovering.
            if member_failures.get(req.msg_id, 0) > 0:
                member_failures[req.msg_id] -= 1
                cb(rid, None, http_error(429, "rateLimitExceeded"))
            elif req.message is None:
                cb(rid, None, http_error(404, "notFound"))
            else:
                cb(rid, req.message, None)


class _Messages:
    def __init__(self, service: "FakeService") -> None:
        self._service = service

    def list(self, **kwargs):
        return _ListRequest(self._service, kwargs)

    def get(self, userId, id, format):  # noqa: A002 - mirror the real signature
        return _GetRequest(self._service, id)

    def batchModify(self, userId, body):  # noqa: N802 - mirror the real API
        return _SimpleRequest(self._service, None, record=("modify", body))


class _Labels:
    def __init__(self, service: "FakeService") -> None:
        self._service = service

    def list(self, userId):  # noqa: N803 - mirror the real signature
        return _SimpleRequest(self._service, {"labels": list(self._service.labels)})

    def create(self, userId, body):  # noqa: N803 - mirror the real signature
        new_id = f"Label_{len(self._service.labels) + 1}"
        label = {"id": new_id, "name": body["name"]}
        self._service.labels.append(label)
        return _SimpleRequest(self._service, label, record=("label_create", body))


class _Filters:
    def __init__(self, service: "FakeService") -> None:
        self._service = service

    def create(self, userId, body):  # noqa: N803 - mirror the real signature
        new_id = f"filter_{len(self._service.created_filters) + 1}"
        self._service.created_filters.append({"id": new_id, **body})
        return _SimpleRequest(self._service, {"id": new_id, **body})

    def delete(self, userId, id):  # noqa: A002, N803 - mirror the real signature
        self._service.deleted_filters.append(id)
        return _SimpleRequest(self._service, None)


class _Settings:
    def __init__(self, service: "FakeService") -> None:
        self._service = service

    def filters(self):
        return _Filters(self._service)


class _HistoryListRequest:
    def __init__(self, service: "FakeService", kwargs: dict) -> None:
        self._service = service
        self._kwargs = kwargs

    def execute(self):
        if self._service.history_404:
            raise http_error(404, "notFound")
        return self._service.history_pages[self._kwargs.get("pageToken")]


class _History:
    def __init__(self, service: "FakeService") -> None:
        self._service = service

    def list(self, **kwargs):
        return _HistoryListRequest(self._service, kwargs)


class _Users:
    def __init__(self, service: "FakeService") -> None:
        self._service = service

    def getProfile(self, userId):  # noqa: N802 - mirror the real API
        return _SimpleRequest(self._service, self._service.profile)

    def messages(self):
        return _Messages(self._service)

    def history(self):
        return _History(self._service)

    def labels(self):
        return _Labels(self._service)

    def settings(self):
        return _Settings(self._service)


class FakeService:
    """In-memory Gmail service. ``pages`` maps a pageToken (None for the first
    page) to a list response dict."""

    def __init__(self, pages: dict, messages_by_id: dict, profile: dict) -> None:
        self.pages = pages
        self.messages_by_id = messages_by_id
        self.profile = profile
        # Injectable transient-failure counters for backoff tests.
        self.list_failures = 0
        self.batch_failures = 0
        self.profile_failures = 0
        # Per-member transient failures: msg_id -> times to fail with 429 before
        # the message's sub-request succeeds. Exercises in-batch retry.
        self.member_failures: dict = {}
        # History API fixtures: pageToken -> response dict; 404 toggle.
        self.history_pages: dict = {}
        self.history_404 = False
        # Records the size of each batch HTTP request executed.
        self.batch_sizes: list = []
        # Page tokens whose list call always fails (simulates a hard stop on a
        # specific page so a prior batch's cursor can be inspected).
        self.fail_list_tokens: set = set()
        # Write-path state (executor): existing labels, created/deleted filters,
        # a log of mutating calls, and transient-failure injection by call kind.
        self.labels: list[dict] = []
        self.created_filters: list[dict] = []
        self.deleted_filters: list[str] = []
        self.calls: list[tuple] = []
        self.call_failures: dict[str, int] = {}

    def users(self):
        return _Users(self)

    def new_batch_http_request(self):
        return _Batch(self)

    def page_for(self, token):
        return self.pages[token]

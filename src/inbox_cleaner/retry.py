"""Shared retry classification for Gmail network calls.

Both the sync engine and the executor wrap their Gmail calls in tenacity
backoff, and "what is worth retrying" is the same in both places:

- a Gmail **rate-limit / transient server error** — HTTP 429, 5xx, or a 403
  whose reason is ``rateLimitExceeded`` (a plain 403 is a real auth/scope
  failure and must surface immediately); or
- a transient **transport-level failure** that never reaches the HTTP layer — a
  connection reset, broken pipe, DNS hiccup, TLS reset, or read timeout. These
  surface as ordinary ``OSError`` / ``ssl`` / ``http.client`` exceptions rather
  than ``HttpError``, so unless they are classified here they slip past the
  backoff and crash the caller (e.g. a thread worker in the triage TUI shows up
  as ``ConnectionResetError: [Errno 104] Connection reset by peer``).
"""

from __future__ import annotations

import http.client
import socket
import ssl

from googleapiclient.errors import HttpError

# HTTP statuses worth retrying. 403 is retried only when its reason is a
# rate-limit one (checked separately) — a plain 403 is a real auth/scope problem.
_RETRY_STATUSES = frozenset({429, 500, 502, 503})
_RATE_LIMIT_REASONS = ("ratelimitexceeded", "userratelimitexceeded")

# Transport-level failures that are transient: retrying on a fresh connection
# stands a good chance of succeeding. All but the ``http.client`` ones are
# ``OSError`` subclasses (``ConnectionResetError`` -> ``ConnectionError`` ->
# ``OSError``). Listing the precise types rather than catching bare ``OSError``
# keeps an unrelated, non-recoverable error from being silently retried/swallowed.
TRANSIENT_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,            # incl. ConnectionReset/BrokenPipe/ConnectionAborted
    TimeoutError,               # incl. socket.timeout (an alias since 3.10)
    socket.gaierror,            # transient DNS failure
    ssl.SSLError,               # TLS reset / unexpected EOF
    http.client.IncompleteRead,
    http.client.BadStatusLine,  # incl. http.client.RemoteDisconnected
)


def is_rate_limit(exc: BaseException) -> bool:
    """True if ``exc`` is a Gmail rate-limit / transient server HTTP error."""
    if not isinstance(exc, HttpError):
        return False
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in _RETRY_STATUSES:
        return True
    if status == 403:
        return any(reason in str(exc).lower() for reason in _RATE_LIMIT_REASONS)
    return False


def is_transient(exc: BaseException) -> bool:
    """True if ``exc`` is worth retrying — a rate limit or a transient transport error."""
    return isinstance(exc, TRANSIENT_NETWORK_ERRORS) or is_rate_limit(exc)

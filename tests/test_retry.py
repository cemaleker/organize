"""Tests for the shared retry classifier.

The sync engine and executor both decide what to retry via
:func:`inbox_cleaner.retry.is_transient`. The key regression here is that a
transport-level failure (e.g. ``ConnectionResetError: [Errno 104]``) — which is
*not* an ``HttpError`` — counts as transient, so the backoff retries it instead
of letting it escape and crash the caller.
"""

import http.client
import socket
import ssl

import pytest

from inbox_cleaner.retry import is_rate_limit, is_transient

from conftest import http_error


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionResetError(104, "Connection reset by peer"),
        BrokenPipeError(32, "Broken pipe"),
        ConnectionAbortedError(),
        TimeoutError("timed out"),
        socket.gaierror("name resolution failed"),
        ssl.SSLError("unexpected eof"),
        http.client.IncompleteRead(b""),
        http.client.RemoteDisconnected("server disconnected"),
    ],
)
def test_transport_errors_are_transient(exc):
    assert is_transient(exc) is True
    # ...but they are not rate limits (no HTTP status to classify).
    assert is_rate_limit(exc) is False


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_rate_limit_statuses_are_transient(status):
    exc = http_error(status, "rateLimitExceeded")
    assert is_rate_limit(exc) is True
    assert is_transient(exc) is True


def test_403_is_transient_only_when_rate_limited():
    assert is_transient(http_error(403, "rateLimitExceeded")) is True
    assert is_transient(http_error(403, "insufficientPermissions")) is False


def test_404_and_unrelated_errors_are_not_transient():
    assert is_transient(http_error(404, "notFound")) is False
    # A genuine bug must not be silently retried/swallowed.
    assert is_transient(ValueError("boom")) is False
    assert is_transient(FileNotFoundError("missing")) is False

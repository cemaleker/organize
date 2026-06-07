"""Message classification and header parsing.

Phase 4 of the plan. Each message is classified once at ingest into a
``(category, group_key)`` pair, which the store aggregates with ``GROUP BY``.

Category precedence, most specific first:

1. ``mailing_list`` — has a ``List-Id`` header. Grouped by the parsed list id
   (e.g. ``commits.github.com``).
2. ``newsletter`` — has ``List-Unsubscribe`` but no ``List-Id``. Grouped by
   full sender address.
3. ``automated`` — ``Precedence: bulk`` or an ``Auto-Submitted`` other than
   ``no`` (auto-replies, system mail). Grouped by full sender address.
4. ``personal`` — everything else. Grouped by full sender address.

The non-list categories group by the *full sender address* (e.g.
``calendar-notification@google.com``) rather than the bare domain, so the many
distinct services behind a shared domain like ``google.com`` land in separate
groups instead of collapsing into one. Mail with no parseable ``From`` falls
back to the domain (``unknown``).

Header names in email are case-insensitive, so :func:`headers_to_dict`
canonicalizes them to hyphen-title-case (``list-id`` -> ``List-Id``); all lookups
here use that canonical form.
"""

from __future__ import annotations

import re
from email.utils import parseaddr

# Matches the bracketed identifier in a List-Id header value, e.g.
# 'Dev List <dev.list.example.com>' -> 'dev.list.example.com'.
_LIST_ID_BRACKETS = re.compile(r"<([^>]+)>")

# Canonical header names the classifier reads. Persisted at ingest so categories
# can be recomputed offline (see Store.reclassify_all).
CLASSIFY_HEADERS = (
    "From",
    "List-Id",
    "List-Unsubscribe",
    "Precedence",
    "Auto-Submitted",
)


def classify_headers_subset(headers: dict[str, str]) -> dict[str, str]:
    """Pick just the headers the classifier needs, for cheap persistence."""
    return {k: headers[k] for k in CLASSIFY_HEADERS if k in headers}


def _canonical(name: str) -> str:
    """Canonical header key: hyphen-title-case (case-insensitive lookup)."""
    return name.strip().title()


def headers_to_dict(headers: list[dict[str, str]]) -> dict[str, str]:
    """Flatten Gmail's ``payload.headers`` list into a canonical-key dict.

    Gmail returns ``[{"name": "From", "value": "..."}, ...]``. Keys are
    canonicalized to hyphen-title-case so lookups are case-insensitive. On
    duplicate names the first occurrence wins.
    """
    out: dict[str, str] = {}
    for h in headers:
        name = h.get("name", "")
        if not name:
            continue
        key = _canonical(name)
        if key not in out:
            out[key] = h.get("value", "")
    return out


def sender_address(headers: dict[str, str]) -> str:
    """Extract the bare email address from the ``From`` header (lower-cased)."""
    _name, addr = parseaddr(headers.get("From", ""))
    return addr.strip().lower()


def sender_domain(headers: dict[str, str]) -> str:
    """Return the domain part of the sender address, or ``"unknown"``."""
    addr = sender_address(headers)
    if "@" in addr:
        return addr.rsplit("@", 1)[-1]
    return "unknown"


def parse_list_id(value: str) -> str:
    """Extract the list identifier from a ``List-Id`` header value.

    Handles the common forms:

    - ``"Project Commits <commits.github.com>"`` -> ``commits.github.com``
    - ``"<commits.github.com>"``                 -> ``commits.github.com``
    - ``"commits.github.com"`` (bare)            -> ``commits.github.com``
    """
    match = _LIST_ID_BRACKETS.search(value)
    raw = match.group(1) if match else value
    return raw.strip().lower()


def classify(headers: dict[str, str]) -> tuple[str, str]:
    """Return ``(category, group_key)`` for a message, by header precedence."""
    list_id = headers.get("List-Id")
    if list_id:
        return "mailing_list", parse_list_id(list_id)

    # Non-list mail groups by full sender address, falling back to the domain
    # (``unknown`` when there is no parseable ``From``) so the key is never empty.
    sender_key = sender_address(headers) or sender_domain(headers)

    if "List-Unsubscribe" in headers:
        return "newsletter", sender_key

    precedence = headers.get("Precedence", "").lower()
    auto_submitted = headers.get("Auto-Submitted", "no").lower()
    if precedence == "bulk" or auto_submitted != "no":
        return "automated", sender_key

    return "personal", sender_key

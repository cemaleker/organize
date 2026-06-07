"""Tests for the header parser and classifier, on header fixtures."""

import pytest

from inbox_cleaner.classify import (
    classify,
    headers_to_dict,
    parse_list_id,
    sender_address,
    sender_domain,
)


def H(**headers):
    """Build a canonical header dict from keyword pairs (underscores -> hyphens)."""
    return headers_to_dict(
        [{"name": k.replace("_", "-"), "value": v} for k, v in headers.items()]
    )


# -- header parsing ---------------------------------------------------------


def test_headers_to_dict_is_case_insensitive():
    d = headers_to_dict(
        [
            {"name": "list-id", "value": "<a.example.com>"},
            {"name": "FROM", "value": "x@y.com"},
        ]
    )
    assert d["List-Id"] == "<a.example.com>"
    assert d["From"] == "x@y.com"


def test_headers_to_dict_first_occurrence_wins():
    d = headers_to_dict(
        [{"name": "Subject", "value": "first"}, {"name": "subject", "value": "second"}]
    )
    assert d["Subject"] == "first"


def test_sender_address_and_domain():
    h = H(From="Alice Example <Alice@Example.COM>")
    assert sender_address(h) == "alice@example.com"
    assert sender_domain(h) == "example.com"


def test_sender_domain_unknown_when_missing():
    assert sender_domain(H(Subject="no from")) == "unknown"


# -- parse_list_id ----------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Project Commits <commits.github.com>", "commits.github.com"),
        ("<commits.github.com>", "commits.github.com"),
        ("commits.github.com", "commits.github.com"),
        ("  <Mixed.Case.COM> ", "mixed.case.com"),
    ],
)
def test_parse_list_id(value, expected):
    assert parse_list_id(value) == expected


# -- classify precedence ----------------------------------------------------


def test_mailing_list_takes_precedence_and_groups_by_list_id():
    # List-Id beats List-Unsubscribe even when both are present.
    h = H(
        From="noreply@github.com",
        List_Id="Repo <repo.commits.github.com>",
        List_Unsubscribe="<mailto:unsub@github.com>",
    )
    assert classify(h) == ("mailing_list", "repo.commits.github.com")


def test_newsletter_when_unsubscribe_but_no_list_id():
    # Non-list mail groups by full sender address, not just the domain.
    h = H(From="news@substack.com", List_Unsubscribe="<mailto:u@substack.com>")
    assert classify(h) == ("newsletter", "news@substack.com")


def test_automated_on_precedence_bulk():
    h = H(From="bot@service.io", Precedence="bulk")
    assert classify(h) == ("automated", "bot@service.io")


def test_automated_on_auto_submitted():
    h = H(From="bot@service.io", Auto_Submitted="auto-replied")
    assert classify(h) == ("automated", "bot@service.io")


def test_auto_submitted_no_is_not_automated():
    h = H(From="real@person.com", Auto_Submitted="no")
    assert classify(h) == ("personal", "real@person.com")


def test_personal_default():
    h = H(From="friend@gmail.com", Subject="lunch?")
    assert classify(h) == ("personal", "friend@gmail.com")


def test_non_list_mail_splits_distinct_services_on_one_domain():
    # The whole point: two Google services on @google.com no longer collapse.
    cal = H(From="Google Calendar <calendar-notification@google.com>", Precedence="bulk")
    sec = H(From="Google <no-reply@accounts.google.com>", Precedence="bulk")
    assert classify(cal) == ("automated", "calendar-notification@google.com")
    assert classify(sec) == ("automated", "no-reply@accounts.google.com")


def test_non_list_mail_without_from_falls_back_to_domain():
    h = H(Precedence="bulk")  # no From header
    assert classify(h) == ("automated", "unknown")


def test_classify_is_case_insensitive_on_header_names():
    h = H(From="news@x.com", list_unsubscribe="<mailto:u@x.com>")
    assert classify(h) == ("newsletter", "news@x.com")

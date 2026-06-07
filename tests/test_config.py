"""Tests for the Config object and CLI argument plumbing.

These run fully offline — no network, no Gmail. They guard the cheap-to-break
wiring: path normalization, scope defaults, and argparse overrides.
"""

from pathlib import Path

from inbox_cleaner.cli import _config_from_args, build_parser
from inbox_cleaner.config import READONLY_SCOPES, Config


def test_defaults_are_readonly_and_local():
    cfg = Config()
    assert cfg.scopes == READONLY_SCOPES
    assert all("readonly" in s for s in cfg.scopes)
    assert cfg.credentials_path == Path("credentials.json")
    assert cfg.token_path == Path("token.json")
    assert cfg.db_path == Path("inbox.sqlite")


def test_string_paths_are_normalized_to_path():
    # Deliberately pass str instead of Path to exercise __post_init__ coercion.
    cfg = Config(credentials_path="c.json", token_path="t.json", db_path="x.db")  # pyright: ignore[reportArgumentType]
    assert isinstance(cfg.credentials_path, Path)
    assert isinstance(cfg.token_path, Path)
    assert isinstance(cfg.db_path, Path)


def test_no_write_scope_requested_by_default():
    # The safety boundary: the analysis path must never carry a write scope.
    cfg = Config()
    assert not any("modify" in s or "mail.google.com" in s for s in cfg.scopes)


def test_cli_overrides_paths():
    args = build_parser().parse_args(
        ["--credentials", "a.json", "--token", "b.json", "--db", "c.db", "auth"]
    )
    cfg = _config_from_args(args)
    assert cfg.credentials_path == Path("a.json")
    assert cfg.token_path == Path("b.json")
    assert cfg.db_path == Path("c.db")
    assert args.command == "auth"

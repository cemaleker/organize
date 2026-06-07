"""OAuth2 authentication and Gmail client construction.

Phase 1 of the plan. Responsibilities:

- Run the installed-app OAuth consent flow once and persist the resulting
  credentials (including the refresh token) to disk.
- On subsequent runs, load the cached credentials and silently refresh the
  access token when it has expired.
- Hand back an authorized Gmail API service object.

All of this uses the read-only scope from :data:`Config.scopes`. The blocking
google-api-python-client lives behind this module so the rest of the codebase
talks to a ready-to-use service.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import Config


class AuthError(RuntimeError):
    """Raised when authentication cannot be completed."""


def _load_cached_credentials(
    token_path: Path, scopes: tuple[str, ...]
) -> Credentials | None:
    """Load persisted credentials if present and scope-compatible."""
    if not token_path.exists():
        return None
    return Credentials.from_authorized_user_file(str(token_path), list(scopes))


def _save_credentials(token_path: Path, creds: Credentials) -> None:
    """Persist credentials (incl. refresh token) for future runs."""
    token_path.write_text(creds.to_json(), encoding="utf-8")


def get_credentials(
    config: Config | None = None,
    *,
    scopes: tuple[str, ...] | None = None,
    token_path: Path | None = None,
) -> Credentials:
    """Return valid OAuth credentials, running the consent flow if needed.

    Order of preference, mirroring the canonical Google sample:

    1. Use cached credentials from ``token_path`` if they are valid.
    2. If they exist but are expired and refreshable, refresh in-place.
    3. Otherwise run the interactive installed-app flow, which opens a browser
       for consent, then persist the result.

    ``scopes`` and ``token_path`` default to the read-only set on the config; the
    executor passes the write scopes and the separate write token so its broader
    consent is cached independently of the read-only one.

    Raises:
        AuthError: if no cached token exists and the client secret file
            (``credentials_path``) is missing, so the flow cannot start.
    """
    config = config or Config()
    scopes = scopes if scopes is not None else config.scopes
    token_path = token_path if token_path is not None else config.token_path
    creds = _load_cached_credentials(token_path, scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_credentials(token_path, creds)
        return creds

    if not config.credentials_path.exists():
        raise AuthError(
            f"OAuth client secret not found at '{config.credentials_path}'. "
            "Download a Desktop-app OAuth client from Google Cloud and save it "
            "there (see SETUP.md)."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.credentials_path), list(scopes)
    )
    # run_local_server is typed as returning a union that includes
    # external-account credentials; the installed-app flow always yields the
    # OAuth2 user Credentials we persist.
    creds = cast(Credentials, flow.run_local_server(port=0))
    _save_credentials(token_path, creds)
    return creds


def get_service(config: Config | None = None):
    """Return an authorized, read-only Gmail API service object.

    The returned object is the standard google-api-python-client ``Resource``;
    it is blocking, so callers that need to stay responsive (the TUI) must run
    it inside a thread worker.
    """
    config = config or Config()
    creds = get_credentials(config)
    # cache_discovery=False avoids noisy warnings and stale on-disk discovery
    # docs; the network discovery fetch is fine for an interactive CLI.
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_write_service(config: Config | None = None):
    """Return an authorized Gmail service with the write-path scopes.

    Uses ``write_scopes`` (``gmail.modify`` + ``gmail.settings.basic``) and the
    separate ``write_token_path``, so consenting to writes never touches the
    read-only token. This is the *only* entry point that yields a service able to
    mutate the mailbox — the executor's sole dependency.
    """
    config = config or Config()
    creds = get_credentials(
        config, scopes=config.write_scopes, token_path=config.write_token_path
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)

"""Central configuration object.

Everything that varies between environments or phases lives here: where secrets
and the local index are stored, which OAuth scopes are requested, and tuning
knobs like the fetch batch size. Paths default to the current working directory
so the tool is usable with zero config, but each is overridable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Read-only scope used for the entire fetch/index/classify path. The write
# scopes are intentionally NOT here — they are requested only by the executor
# component, against a separate token, so the analysis path can never mutate.
READONLY_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.readonly",)

# Write-path scopes, requested only by the executor and cached in a separate
# token file. ``gmail.modify`` can label/archive/trash (never permanent-delete);
# ``gmail.settings.basic`` can create the filters that handle future mail.
WRITE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
)


@dataclass(slots=True)
class Config:
    """Runtime configuration for the read/analysis path.

    Attributes:
        credentials_path: OAuth client secret JSON downloaded from Google Cloud
            (a "Desktop app" client). See SETUP.md. Never committed.
        token_path: Where the persisted user credentials (incl. refresh token)
            are cached after the first consent flow. Never committed.
        db_path: SQLite index location.
        scopes: OAuth scopes to request. Defaults to read-only.
        batch_size: Number of message ids fetched per metadata batch request.
    """

    credentials_path: Path = field(default_factory=lambda: Path("credentials.json"))
    token_path: Path = field(default_factory=lambda: Path("token.json"))
    write_token_path: Path = field(default_factory=lambda: Path("token-write.json"))
    db_path: Path = field(default_factory=lambda: Path("inbox.sqlite"))
    scopes: tuple[str, ...] = READONLY_SCOPES
    write_scopes: tuple[str, ...] = WRITE_SCOPES
    batch_size: int = 100

    def __post_init__(self) -> None:
        # Normalize to Path so callers may pass plain strings.
        self.credentials_path = Path(self.credentials_path)
        self.token_path = Path(self.token_path)
        self.write_token_path = Path(self.write_token_path)
        self.db_path = Path(self.db_path)

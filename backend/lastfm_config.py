"""Last.fm credentials loaded from the process environment or project .env file."""

from __future__ import annotations

import logging
import os

from .config import ROOT

log = logging.getLogger("kadr.lastfm")
ENV_FILE = ROOT / ".env"


def get_lastfm_api_key() -> str | None:
    """Return the Last.fm API key without requiring dotenv as a dependency."""
    key = os.environ.get("LASTFM_API_KEY", "").strip()
    if not key and ENV_FILE.is_file():
        try:
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                entry = line.strip()
                if not entry or entry.startswith("#"):
                    continue
                if entry.startswith("export "):
                    entry = entry[7:].strip()
                name, separator, value = entry.partition("=")
                if separator and name.strip() == "LASTFM_API_KEY":
                    key = value.strip().strip("\"'")
                    break
        except OSError as exc:
            log.warning("Could not read Last.fm .env file %s: %s", ENV_FILE, exc)

    if not key:
        log.warning("LASTFM_API_KEY is not configured; Last.fm requests are disabled")
        return None
    return key

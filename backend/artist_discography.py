"""Recent MusicBrainz release groups for an artist."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from .config import ROOT, USER_AGENT

API_URL = "https://musicbrainz.org/ws/2/release-group/"
CACHE_TTL_SECONDS = 24 * 60 * 60
_request_lock = asyncio.Lock()
_last_request_at = 0.0
log = logging.getLogger("kadr.musicbrainz")


async def fetch_artist_releases(
    client: httpx.AsyncClient, mbid: str, months: int = 3
) -> list[dict[str, Any]]:
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        mbid,
    ):
        raise ValueError("Invalid MusicBrainz artist ID")

    today = date.today()
    start = today - timedelta(days=max(1, min(months, 12)) * 30)
    cache_path = ROOT / ".tools" / "musicbrainz_artist_releases" / (
        hashlib.sha256(f"{mbid}:{start}:{today}".encode()).hexdigest() + ".json"
    )
    cached = _read_cache(cache_path)
    if cached is not None:
        return cached

    await _throttle()
    response = await client.get(
        API_URL,
        params={
            "query": f"arid:{mbid} AND date:[{start.isoformat()} TO {today.isoformat()}]",
            "fmt": "json",
            "limit": "100",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=10.0,
    )
    response.raise_for_status()
    payload = response.json()
    groups = payload.get("release-groups", [])
    if not isinstance(groups, list):
        raise ValueError("MusicBrainz returned invalid release groups")

    releases: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        release_date = str(group.get("first-release-date") or "")
        try:
            if date.fromisoformat(release_date[:10]) < start:
                continue
        except ValueError:
            continue
        release_id = str(group.get("id") or "")
        if not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            release_id,
        ):
            continue
        releases.append(
            {
                "id": release_id,
                "title": str(group.get("title") or "Без названия"),
                "date": release_date,
                "type": str(group.get("primary-type") or ""),
                "url": f"https://musicbrainz.org/release-group/{release_id}",
            }
        )

    releases.sort(key=lambda item: item["date"], reverse=True)
    _write_cache(cache_path, releases)
    return releases


async def _throttle() -> None:
    global _last_request_at
    async with _request_lock:
        delay = 1.0 - (time.monotonic() - _last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)
        _last_request_at = time.monotonic()


def _read_cache(path: Path) -> list[dict[str, Any]] | None:
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Ignoring unreadable MusicBrainz cache %s: %s", path, exc)
        return None
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("cached_at"), (int, float))
        or not isinstance(entry.get("releases"), list)
        or time.time() - entry["cached_at"] >= CACHE_TTL_SECONDS
    ):
        return None
    return [item for item in entry["releases"] if isinstance(item, dict)]


def _write_cache(path: Path, releases: list[dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"cached_at": time.time(), "releases": releases},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        log.error("Could not write MusicBrainz artist cache %s: %s", path, exc)

"""Recent release lookup for followed library artists via Deezer's public catalogue."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

API = "https://api.deezer.com"
MAX_FOLLOWED_ARTISTS = 12
MAX_RELEASES_PER_ARTIST = 50


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", " ", value).strip()


def _exact_artist(results: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    wanted = name.strip().casefold()
    matches = [item for item in results if str(item.get("name") or "").strip().casefold() == wanted]
    if not matches:
        return None
    return max(matches, key=lambda item: int(item.get("nb_fan") or 0))


async def _artist_releases(
    client: httpx.AsyncClient,
    name: str,
    cutoff: date,
    today: date,
    semaphore: asyncio.Semaphore,
) -> tuple[list[dict[str, Any]], str | None]:
    async with semaphore:
        try:
            search = await client.get(
                f"{API}/search/artist",
                params={"q": name},
                timeout=7.0,
            )
            search.raise_for_status()
            artist = _exact_artist(search.json().get("data") or [], name)
            if not artist:
                return [], None

            albums_response = await client.get(
                f"{API}/artist/{artist['id']}/albums",
                params={"limit": MAX_RELEASES_PER_ARTIST},
                timeout=7.0,
            )
            albums_response.raise_for_status()
            albums = albums_response.json().get("data") or []
        except Exception:
            return [], f"Не удалось проверить релизы: {name}"

    releases: list[dict[str, Any]] = []
    for album in albums:
        raw_date = str(album.get("release_date") or "")
        try:
            release_date = date.fromisoformat(raw_date[:10])
        except ValueError:
            continue
        if not cutoff <= release_date <= today:
            continue
        link = str(album.get("link") or "")
        if not link.startswith("https://www.deezer.com/"):
            continue
        releases.append(
            {
                "id": str(album.get("id") or ""),
                "artist": str(artist.get("name") or name),
                "title": str(album.get("title") or "Без названия"),
                "date": release_date.isoformat(),
                "url": link,
                "cover": str(album.get("cover_medium") or ""),
                "type": str(album.get("record_type") or "release"),
                "explicit": bool(album.get("explicit_lyrics")),
                "source": "Deezer",
            }
        )
    return releases, None


async def _single_tracks(
    client: httpx.AsyncClient,
    release: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    if not release.get("id"):
        return []
    async with semaphore:
        try:
            response = await client.get(
                f"{API}/album/{release['id']}/tracks",
                params={"limit": 100},
                timeout=7.0,
            )
            response.raise_for_status()
            data = response.json().get("data") or []
        except (httpx.HTTPError, ValueError):
            return []
    rows: list[dict[str, Any]] = []
    for track in data:
        preview = str(track.get("preview") or "")
        preview_host = (urlparse(preview).hostname or "").casefold()
        if preview and not (
            urlparse(preview).scheme == "https"
            and (preview_host.endswith(".deezer.com") or preview_host.endswith(".dzcdn.net"))
        ):
            preview = ""
        rows.append(
            {
                "artist": release["artist"],
                "title": str(track.get("title") or release["title"]),
                "date": release["date"],
                "url": str(track.get("link") or release["url"]),
                "preview": preview,
                "cover": release["cover"],
                "source": "Deezer",
                "release_type": release["type"],
            }
        )
    return rows


async def fetch_weekly_releases(
    client: httpx.AsyncClient,
    artist_names: list[str],
    days: int = 7,
    local_tracks: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Return new Deezer singles/tracks and albums, excluding local recordings."""
    names = list(dict.fromkeys(name.strip() for name in artist_names if isinstance(name, str) and name.strip()))
    names = names[:MAX_FOLLOWED_ARTISTS]
    today = date.today()
    cutoff = today - timedelta(days=max(1, min(int(days), 14)))
    semaphore = asyncio.Semaphore(3)
    results = await asyncio.gather(
        *(_artist_releases(client, name, cutoff, today, semaphore) for name in names)
    )
    releases = [release for group, _ in results for release in group]
    releases.sort(key=lambda item: (item["date"], item["artist"].casefold()), reverse=True)
    errors = [error for _, error in results if error]
    local = {
        (_normalize(row.get("artist", "")), _normalize(row.get("title", "")))
        for row in local_tracks or []
    }
    local_albums = {
        (_normalize(row.get("artist", "")), _normalize(row.get("album", "")))
        for row in local_tracks or []
        if row.get("album")
    }
    albums = [
        {key: value for key, value in release.items() if key != "id"}
        for release in releases
        if release["type"] != "single"
        and (_normalize(release["artist"]), _normalize(release["title"])) not in local_albums
    ]
    release_tracks = await asyncio.gather(
        *(_single_tracks(client, release, semaphore) for release in releases)
    )
    tracks: list[dict[str, Any]] = []
    seen_tracks: set[tuple[str, str]] = set()
    for group in release_tracks:
        for track in group:
            key = (_normalize(track["artist"]), _normalize(track["title"]))
            if key in local or key in seen_tracks:
                continue
            seen_tracks.add(key)
            tracks.append(track)
    tracks.sort(key=lambda item: (item["date"], item["artist"].casefold()), reverse=True)
    return {
        "source": "Deezer",
        "from": cutoff.isoformat(),
        "to": today.isoformat(),
        "checked": len(names),
        "tracks": tracks,
        "albums": albums,
        "errors": errors,
    }

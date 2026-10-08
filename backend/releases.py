"""Recent release lookup for followed library artists via Deezer's public catalogue."""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

API = "https://api.deezer.com"
ITUNES_API = "https://itunes.apple.com/search"
MAX_RELEASES_PER_ARTIST = 50
MAX_LASTFM_FALLBACK_ARTISTS = 6
log = logging.getLogger("kadr.weekly")


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
            log.info("Weekly request: provider=Deezer endpoint=search/artist artist=%s status=%d", name, search.status_code)
            search.raise_for_status()
            artist = _exact_artist(search.json().get("data") or [], name)
            if not artist:
                return [], None

            albums_response = await client.get(
                f"{API}/artist/{artist['id']}/albums",
                params={"limit": MAX_RELEASES_PER_ARTIST},
                timeout=7.0,
            )
            log.info("Weekly request: provider=Deezer endpoint=artist/albums artist=%s status=%d", name, albums_response.status_code)
            albums_response.raise_for_status()
            albums = albums_response.json().get("data") or []
        except Exception as exc:
            log.warning("Deezer release lookup failed for %s: %s", name, type(exc).__name__)
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


async def _itunes_artist_tracks(
    client: httpx.AsyncClient,
    name: str,
    cutoff: date,
    today: date,
    semaphore: asyncio.Semaphore,
) -> tuple[list[dict[str, Any]], str | None]:
    async with semaphore:
        try:
            response = await client.get(
                ITUNES_API,
                params={"term": name, "entity": "musicTrack", "limit": 10},
                timeout=7.0,
            )
            log.info("Weekly request: provider=iTunes endpoint=search artist=%s status=%d", name, response.status_code)
            response.raise_for_status()
            results = response.json().get("results") or []
        except Exception as exc:
            log.warning("iTunes release lookup failed for %s: %s", name, type(exc).__name__)
            return [], f"Не удалось проверить iTunes: {name}"

    wanted = _normalize(name)
    tracks: list[dict[str, Any]] = []
    for item in results:
        artist = str(item.get("artistName") or "").strip()
        title = str(item.get("trackName") or "").strip()
        artist_key = _normalize(artist)
        if not artist or not title or wanted not in artist_key:
            continue
        try:
            released = date.fromisoformat(str(item.get("releaseDate") or "")[:10])
        except ValueError:
            continue
        if not cutoff <= released <= today:
            continue
        url = str(item.get("trackViewUrl") or "")
        if not url.startswith("https://music.apple.com/"):
            continue
        preview = str(item.get("previewUrl") or "")
        if preview and not preview.startswith("https://"):
            preview = ""
        tracks.append(
            {
                "artist": artist,
                "title": title,
                "date": released.isoformat(),
                "url": url,
                "preview": preview,
                "cover": str(item.get("artworkUrl100") or ""),
                "source": "iTunes",
                "release_type": "single",
            }
        )
    return tracks, None


async def _lastfm_artist_tracks(
    lastfm: Any,
    name: str,
    cutoff: date,
    today: date,
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        candidates = await lastfm.get_artist_top_tracks(name, limit=10)
    except Exception as exc:
        log.warning("Last.fm release lookup failed for %s: %s", name, type(exc).__name__)
        return [], f"Не удалось проверить Last.fm: {name}"

    tracks: list[dict[str, Any]] = []
    for item in candidates:
        album = item.get("album") if isinstance(item.get("album"), dict) else {}
        raw_date = item.get("releaseDate") or item.get("releasedate") or album.get("releasedate")
        try:
            released = date.fromisoformat(str(raw_date or "")[:10])
        except ValueError:
            continue
        if not cutoff <= released <= today:
            continue
        artist_data = item.get("artist")
        artist = str(artist_data.get("name") or name) if isinstance(artist_data, dict) else name
        title = str(item.get("name") or "").strip()
        url = str(item.get("url") or "")
        if not title or not url.startswith("https://www.last.fm/"):
            continue
        tracks.append(
            {
                "artist": artist,
                "title": title,
                "date": released.isoformat(),
                "url": url,
                "preview": "",
                "cover": "",
                "source": "Last.fm",
                "release_type": "single",
            }
        )
    log.info(
        "Weekly request: provider=Last.fm endpoint=artist.getTopTracks artist=%s status=ok releases=%d",
        name,
        len(tracks),
    )
    return tracks, None


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
            log.info("Weekly request: provider=Deezer endpoint=album/tracks artist=%s status=%d", release.get("artist", ""), response.status_code)
            response.raise_for_status()
            data = response.json().get("data") or []
        except (httpx.HTTPError, ValueError) as exc:
            log.warning(
                "Deezer track lookup failed for release %s: %s",
                release["id"],
                type(exc).__name__,
            )
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
    lastfm: Any | None = None,
) -> dict[str, Any]:
    """Return new Deezer singles/tracks and albums, excluding local recordings."""
    names = list(dict.fromkeys(name.strip() for name in artist_names if isinstance(name, str) and name.strip()))
    today = date.today()
    cutoff = today - timedelta(days=max(1, min(int(days), 30)))
    semaphore = asyncio.Semaphore(8)
    results = await asyncio.gather(
        *(_artist_releases(client, name, cutoff, today, semaphore) for name in names)
    )
    releases = [release for group, _ in results for release in group]
    releases.sort(key=lambda item: (item["date"], item["artist"].casefold()), reverse=True)
    errors = [error for _, error in results if error]
    empty_artists = [name for name, (group, _) in zip(names, results) if not group]
    itunes_results = await asyncio.gather(
        *(_itunes_artist_tracks(client, name, cutoff, today, semaphore) for name in empty_artists)
    )
    itunes_tracks = [track for group, _ in itunes_results for track in group]
    errors.extend(error for _, error in itunes_results if error)
    lastfm_tracks: list[dict[str, Any]] = []
    if lastfm is not None and not releases and not itunes_tracks:
        fallback_artists = names[:MAX_LASTFM_FALLBACK_ARTISTS]
        lastfm_results = await asyncio.gather(
            *(
                _lastfm_artist_tracks(lastfm, name, cutoff, today)
                for name in fallback_artists
            )
        )
        lastfm_tracks = [track for group, _ in lastfm_results for track in group]
        errors.extend(error for _, error in lastfm_results if error)
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
        *(
            _single_tracks(client, release, semaphore)
            for release in releases
            if release["type"] == "single"
        )
    )
    seen_tracks: set[tuple[str, str]] = set()
    tracks: list[dict[str, Any]] = []
    for track in [*itunes_tracks, *lastfm_tracks]:
        key = (_normalize(track["artist"]), _normalize(track["title"]))
        if key in local or key in seen_tracks:
            continue
        seen_tracks.add(key)
        tracks.append(track)
    for group in release_tracks:
        for track in group:
            key = (_normalize(track["artist"]), _normalize(track["title"]))
            if key in local or key in seen_tracks:
                continue
            seen_tracks.add(key)
            tracks.append(track)
    tracks.sort(key=lambda item: (item["date"], item["artist"].casefold()), reverse=True)
    return {
        "source": "Deezer + fallback" if itunes_tracks or lastfm_tracks else "Deezer",
        "from": cutoff.isoformat(),
        "to": today.isoformat(),
        "checked": len(names),
        "tracks": tracks,
        "albums": albums,
        "errors": errors,
    }

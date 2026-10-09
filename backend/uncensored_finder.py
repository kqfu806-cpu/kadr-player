"""Find likely official, uncensored YouTube uploads without downloading them."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from rapidfuzz.fuzz import ratio

from .config import ROOT
from .scanner import Track

AUDIODL_PATH = ROOT / "tools" / "audiodl.exe"
AUDIODL_FALLBACK = "yt-dlp"


log = logging.getLogger("kadr.uncensored")
_EDITED = re.compile(r"\b(?:clean|radio[\s-]+edit|edited|censored)\b", re.I)
_NON_ORIGINAL = re.compile(
    r"\b(?:live|cover|piano|instrumental|karaoke|remix(?:ed|es)?|bootleg|mashup|"
    r"edit(?:ed|s)?|version(?:s)?|slowed|nightcore|acoustic|reverb)\b|sped[\s-]*up",
    re.I,
)
_TITLE_NOISE = re.compile(
    r"\b(?:official(?:\s+(?:audio|video|music\s+video))?|audio|lyrics?|"
    r"lyric\s+video|visualizer|explicit|hd|4k)\b",
    re.I,
)


def _official_channel(channel: str, artist: str) -> bool:
    if re.search(r"(?:vevo|topic)", channel, re.I):
        return True
    artist_norm = re.sub(r"\W+", " ", artist.casefold()).strip()
    channel_norm = re.sub(r"\W+", " ", channel.casefold()).strip()
    if not artist_norm:
        return False
    return channel_norm == artist_norm or (
        artist_norm in channel_norm and bool(re.search(r"\bofficial\b", channel_norm))
    )


def _resolve_audiodl() -> str:
    if AUDIODL_PATH.is_file():
        return str(AUDIODL_PATH)
    return AUDIODL_FALLBACK


def _search(query: str) -> list[dict[str, Any]]:
    """Use tools/audiodl.exe --dump-json for ytsearch, fallback to yt-dlp."""
    base_cmd = [_resolve_audiodl(), "--dump-json", "--skip-download", "--no-warnings", "--no-playlist"]
    # audiodl.exe is yt-dlp wrapper; use ytsearch prefix
    command = [*base_cmd, f"ytsearch3:{query}"]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    if result.returncode and not result.stdout.strip():
        raise RuntimeError(result.stderr.strip() or "yt-dlp search failed")
    if not result.stdout.strip():
        return []
    # --dump-json with ytsearch may produce JSON lines per entry
    entries: list[dict[str, Any]] = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("id"):
            entries.append(data)
        elif isinstance(data, dict) and data.get("entries"):
            for e in data.get("entries") or []:
                if isinstance(e, dict):
                    entries.append(e)
    return entries


def _candidate_url(entry: dict[str, Any]) -> str:
    webpage = entry.get("webpage_url") or entry.get("url") or ""
    if str(webpage).startswith(("http://", "https://")):
        return str(webpage)
    video_id = entry.get("id")
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""


def _title_matches(candidate_title: str, track: Track) -> bool:
    candidate = re.sub(re.escape(track.artist), " ", candidate_title, flags=re.I)
    candidate = _TITLE_NOISE.sub(" ", candidate)
    normalize = lambda value: re.sub(r"\W+", " ", value.casefold()).strip()
    expected = normalize(track.title)
    actual = normalize(candidate)
    return bool(expected and actual and ratio(expected, actual) >= 70)


def _rank(entry: dict[str, Any], track: Track, canonical_duration: int) -> tuple[float, dict[str, Any]] | None:
    title = str(entry.get("title") or "")
    channel = str(entry.get("channel") or entry.get("uploader") or "")
    duration = entry.get("duration")
    url = _candidate_url(entry)
    if (
        not title
        or not channel
        or not url
        or _EDITED.search(title)
        or _NON_ORIGINAL.search(title)
    ):
        return None
    if not _title_matches(title, track):
        return None
    if not _official_channel(channel, track.artist):
        return None
    if not isinstance(duration, (int, float)) or canonical_duration <= 0:
        return None
    if abs(duration - canonical_duration) / canonical_duration > 0.03:
        return None

    norm = re.sub(r"\W+", " ", f"{title} {channel}".casefold())
    score = sum(2 for word in re.findall(r"\w+", track.artist.casefold()) if word in norm)
    score += sum(1 for word in re.findall(r"\w+", track.title.casefold()) if word in norm)
    score += 4 if _official_channel(channel, track.artist) else 0
    score += max(0, 2 - 2 * abs(duration - canonical_duration) / canonical_duration)
    return score, {
        "title": title,
        "channel": channel,
        "duration": round(duration),
        "url": url,
    }


async def find_candidates(track: Track, canonical_duration: int | None) -> list[dict[str, Any]]:
    if not canonical_duration:
        return []
    queries = [
        f"{track.artist} - {track.title} official audio",
        f"{track.artist} - {track.title} explicit",
        f"{track.artist} - {track.title} album version",
    ]
    candidates: dict[str, tuple[float, dict[str, Any]]] = {}
    for query in queries:
        try:
            entries = await asyncio.to_thread(_search, query)
        except (OSError, subprocess.SubprocessError, RuntimeError, json.JSONDecodeError) as exc:
            log.warning("yt-dlp search failed for %r: %s", query, exc)
            continue
        for entry in entries:
            ranked = _rank(entry, track, canonical_duration)
            if ranked:
                score, candidate = ranked
                previous = candidates.get(candidate["url"])
                if previous is None or score > previous[0]:
                    candidates[candidate["url"]] = (score, candidate)
    return [row for _, row in sorted(candidates.values(), key=lambda item: item[0], reverse=True)[:3]]

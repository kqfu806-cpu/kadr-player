"""Поиск видео на YouTube без API-ключа (InnerTube + страница выдачи)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import quote_plus

import httpx

from .config import USER_AGENT, YOUTUBE_CANDIDATES

# Публичный client-key веб-плеера YouTube (лежит в исходниках youtube.com, это не секрет разработчика)
_WEB_CLIENT_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "*/*",
}


@dataclass
class YTCandidate:
    video_id: str
    title: str
    channel: str
    duration_sec: int
    views: str
    badges: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_duration(text: str | None) -> int:
    """'3:45' / '1:02:33' → секунды."""
    if not text:
        return 0
    text = text.strip()
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    return 0


def _walk(obj: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(obj, dict):
        if key in obj:
            found.append(obj[key])
        for v in obj.values():
            found.extend(_walk(v, key))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk(item, key))
    return found


def _runs_text(node: Any) -> str:
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "simpleText" in node:
            return str(node["simpleText"])
        runs = node.get("runs")
        if isinstance(runs, list):
            return "".join(str(r.get("text", "")) for r in runs if isinstance(r, dict))
    return ""


def _from_renderer(vr: dict) -> YTCandidate | None:
    vid = vr.get("videoId")
    if not vid:
        return None
    title = _runs_text(vr.get("title"))
    channel = _runs_text(vr.get("ownerText") or vr.get("longBylineText"))
    length = _runs_text(vr.get("lengthText"))
    views = _runs_text(vr.get("viewCountText"))
    badges = []
    for b in vr.get("badges") or []:
        if isinstance(b, dict):
            badges.append(_runs_text(b.get("metadataBadgeRenderer", {}).get("label")))
    return YTCandidate(
        video_id=str(vid),
        title=title or "(без названия)",
        channel=channel or "",
        duration_sec=parse_duration(length),
        views=views or "",
        badges=", ".join(x for x in badges if x),
    )


def _dedupe(items: list[YTCandidate]) -> list[YTCandidate]:
    seen: set[str] = set()
    out: list[YTCandidate] = []
    for it in items:
        if it.video_id in seen:
            continue
        seen.add(it.video_id)
        out.append(it)
    return out


async def _innertube_search(client: httpx.AsyncClient, query: str) -> list[YTCandidate]:
    url = "https://www.youtube.com/youtubei/v1/search"
    payload = {
        "context": {
            "client": {
                "hl": "ru",
                "gl": "RU",
                "clientName": "WEB",
                "clientVersion": "2.20250301.00.00",
            }
        },
        "query": query,
    }
    r = await client.post(
        url,
        params={"key": _WEB_CLIENT_KEY, "prettyPrint": "false"},
        json=payload,
        headers={**_HEADERS, "Content-Type": "application/json"},
        timeout=20.0,
    )
    r.raise_for_status()
    data = r.json()
    renderers = _walk(data, "videoRenderer")
    return [c for c in (_from_renderer(x) for x in renderers) if c]


async def _html_search(client: httpx.AsyncClient, query: str) -> list[YTCandidate]:
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&hl=ru&gl=RU"
    r = await client.get(url, headers=_HEADERS, timeout=20.0, follow_redirects=True)
    r.raise_for_status()
    html = r.text
    m = re.search(r"ytInitialData\s*=\s*(\{.*?\});\s*</script>", html, re.S)
    if not m:
        m = re.search(r"ytInitialData\"\s*:\s*(\{.*?\}),\s*\"ytInitialPlayer", html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    renderers = _walk(data, "videoRenderer")
    return [c for c in (_from_renderer(x) for x in renderers) if c]


_BAD_TITLE = re.compile(
    r"\b(lyrics?|lyric video|audio(?:\s+only)?|visualizer|karaoke|live(?:\s+performance)?|concert|"
    r"official\s+release|release|релиз|концерт|лайв|аудио|караоке|radio|радио|"
    r"nightcore|slowed|sped\s+up|speed[\s-]?up|remix|ремикс|reaction|cover\b|"
    r"ai[\s-]*cover|suno|leak|слив|preview|snippet|demo|демо|"
    r"minus|минус|instrumental|инструментал|"
    r"на русском|fan[\s-]*made|8d)\b",
    re.I,
)
_OFFICIAL_CLIP = re.compile(
    r"\bofficial\s*(?:music\s*)?(?:video|clip)\b|"
    r"официальн\w*\s*(?:музыкальн\w*\s*)?(?:клип|видео)|\bvevo\b",
    re.I,
)
_OFFICIAL_CHANNEL = re.compile(r"official|vevo|records|record|label|лейбл|topic|официал", re.I)
_DATED_EVENT = re.compile(r"\b(?:19|20)\d{2}[-./]\d{1,2}[-./]\d{1,2}\b")
_TITLE_STOPWORDS = {"a", "an", "the", "and", "if", "i", "to", "of", "я", "и", "не", "в", "на", "с"}
_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.I)


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text or "")}


def _looks_like_music_clip(
    c: YTCandidate, track_duration: float, artist: str = "", title: str = ""
) -> bool:
    """Грубая отсечка явно неподходящих роликов до ИИ."""
    candidate_title = c.title or ""
    if _BAD_TITLE.search(candidate_title):
        return False
    if re.search(r"\btopic\b|тема", c.channel or "", re.I):
        return False
    if _DATED_EVENT.search(candidate_title):
        return False
    if re.search(r"\b(single|full song|album version)\b", candidate_title, re.I) and not _OFFICIAL_CLIP.search(candidate_title):
        return False
    # Shorts короче 40 с почти никогда не официальный клип (кроме очень коротких треков)
    if c.duration_sec > 0:
        if c.duration_sec < 40 and (not track_duration or track_duration > 60):
            return False
        if c.duration_sec > 20 * 60 and (not track_duration or track_duration < 12 * 60):
            return False

    wanted = _tokens(title) - _TITLE_STOPWORDS
    if wanted:
        overlap = len(wanted & _tokens(candidate_title)) / len(wanted)
        if overlap < 0.67:
            return False
    channel_tokens = _tokens(c.channel)
    artist_tokens = _tokens(artist)
    artist_channel_match = bool(artist_tokens) and len(artist_tokens & channel_tokens) / len(artist_tokens) >= 0.5
    if not (_OFFICIAL_CLIP.search(candidate_title) or _OFFICIAL_CHANNEL.search(c.channel or "") or artist_channel_match):
        return False
    return True


def _candidate_rank(c: YTCandidate, artist: str, title: str, duration: float) -> tuple[float, int]:
    wanted = _tokens(title) - _TITLE_STOPWORDS
    overlap = len(wanted & _tokens(c.title)) / len(wanted) if wanted else 0.0
    channel = c.channel or ""
    trusted_channel = bool(_OFFICIAL_CHANNEL.search(channel))
    if artist and _tokens(artist) & _tokens(channel):
        trusted_channel = True
    score = overlap * 10
    if _OFFICIAL_CLIP.search(c.title):
        score += 4
    if trusted_channel:
        score += 2
    if duration > 0 and c.duration_sec > 0:
        delta = abs(c.duration_sec - duration)
        score += max(0, 2 - delta / max(duration * 0.25, 30))
    return score, -len(c.title)


async def search_youtube(
    client: httpx.AsyncClient,
    artist: str,
    title: str,
    track_duration: float = 0.0,
    limit: int = YOUTUBE_CANDIDATES,
) -> list[YTCandidate]:
    """Ищем клип несколькими запросами, склеиваем уникальные результаты."""
    queries = [
        f'"{artist}" "{title}" official music video',
        f'"{artist}" "{title}" официальный клип',
        f'{artist} "{title}" official clip',
        f"{artist} {title}",
    ]
    collected: list[YTCandidate] = []
    for q in queries:
        batch: list[YTCandidate] = []
        try:
            batch = await _innertube_search(client, q)
        except Exception:
            batch = []
        if not batch:
            try:
                batch = await _html_search(client, q)
            except Exception:
                batch = []
        collected.extend(batch)

    uniq = _dedupe(collected)
    filtered = [c for c in uniq if _looks_like_music_clip(c, track_duration, artist, title)]
    filtered.sort(key=lambda c: _candidate_rank(c, artist, title, track_duration), reverse=True)
    return filtered[:limit]


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def youtube_embed_url(video_id: str) -> str:
    return f"https://www.youtube.com/embed/{video_id}"


def youtube_thumb(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

"""Текст: .lrc → LRCLIB → зеркала → Ollama (последним, 20с). Genius выключен."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .config import CACHE_DIR, OLLAMA_LLM, OLLAMA_LYRICS_TIMEOUT, USER_AGENT
from .ollama_ai import OllamaClient, extract_json
from .scanner import Track

log = logging.getLogger("kadr.lyrics")

LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_SEARCH = "https://lrclib.net/api/search"
LYRICS_DIR = CACHE_DIR / "lyrics"
NEGATIVE_CACHE_TTL = 6 * 60 * 60

_LRC_LINE = re.compile(r"\[(\d{1,2}):(\d{1,2}(?:[.,]\d{1,3})?)\](.*)")
_LRC_WORD = re.compile(r"<(\d{1,2}):(\d{1,2}(?:[.,]\d{1,3})?)>([^\s<]*)")
_UA = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.8"}
_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def _stamp(mm: str, ss: str) -> float:
    return int(mm) * 60 + float(ss.replace(",", "."))


def parse_lrc(text: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        m = _LRC_LINE.search(raw)
        if not m:
            continue
        t0 = round(_stamp(m.group(1), m.group(2)), 3)
        body = m.group(3).strip()
        if not body:
            continue
        words = []
        for wm in _LRC_WORD.finditer(body):
            wtxt = wm.group(3).strip()
            if not wtxt:
                continue
            words.append({"t": round(_stamp(wm.group(1), wm.group(2)), 3), "text": wtxt})
        plain = _LRC_WORD.sub(lambda x: (x.group(3) or "") + " ", body)
        plain = re.sub(r"\s+", " ", plain).strip()
        lines.append({"t": t0, "text": plain or body, "words": words})
    lines.sort(key=lambda x: x["t"])
    return lines


def _plain_lines(text: str) -> list[dict[str, Any]]:
    out = []
    for row in (text or "").splitlines():
        row = row.strip()
        if row and not row.startswith("["):
            out.append({"t": None, "text": row, "words": []})
    return out


def _pack(source: str, lines: list[dict[str, Any]], synced: bool) -> dict[str, Any]:
    is_synced = bool(synced and lines and lines[0]["t"] is not None)
    return {
        "ok": bool(lines),
        "source": source,
        "synced": is_synced,
        "timing": "timestamped" if is_synced else "none",
        "lines": lines,
    }


def _normalize_identity(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def lyrics_cache_key(track: Track) -> str:
    """Stable SHA-256 key shared by files with the same recording metadata."""
    duration = max(0, int(round(track.duration or 0)))
    identity = "\0".join(
        (
            "lyrics-v1",
            _normalize_identity(track.artist),
            _normalize_identity(track.title),
            _normalize_identity(track.album),
            str(duration),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _cache_path(cache_key: str) -> Path:
    LYRICS_DIR.mkdir(parents=True, exist_ok=True)
    return LYRICS_DIR / f"{cache_key}.json"


def lyrics_cache_path(track: Track) -> Path:
    return _cache_path(lyrics_cache_key(track))


def load_cached(track: Track) -> dict[str, Any] | None:
    key = lyrics_cache_key(track)
    try:
        path = _cache_path(key)
    except OSError as exc:
        log.warning("Could not access lyrics cache for %s: %s", key[:12], exc)
        return None
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            isinstance(data, dict)
            and data.get("cache_key") == key
            and isinstance(data.get("lines"), list)
        ):
            return data
    except (OSError, json.JSONDecodeError):
        return None
    return None


def save_cached(track: Track, payload: dict[str, Any]) -> dict[str, Any]:
    key = lyrics_cache_key(track)
    cached = {**payload, "cache_key": key}
    temporary: Path | None = None
    try:
        path = _cache_path(key)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
    except OSError as exc:
        log.warning("Could not save lyrics cache for %s: %s", key[:12], exc)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                log.debug("Could not remove temporary lyrics cache: %s", cleanup_exc)
    return cached


def _estimate_line_timing(
    payload: dict[str, Any], duration: float | None
) -> dict[str, Any]:
    result = {**payload}
    lines = [dict(line) for line in payload.get("lines", []) if isinstance(line, dict)]
    result["lines"] = lines
    if not result.get("ok") or not lines:
        result["timing"] = "none"
        result["synced"] = False
        return result
    if result.get("timing") == "estimated":
        return result
    if result.get("synced") and all(line.get("t") is not None for line in lines):
        result["timing"] = "timestamped"
        return result

    total_duration = float(duration or 0)
    if total_duration <= 0:
        result["timing"] = "none"
        result["synced"] = False
        return result

    weights = [
        max(1, len(re.findall(r"\w+", str(line.get("text") or ""))))
        for line in lines
    ]
    total_weight = sum(weights)
    elapsed_weight = 0
    for line, weight in zip(lines, weights):
        line["t"] = round(total_duration * elapsed_weight / total_weight, 3)
        line["words"] = []
        elapsed_weight += weight
    result["timing"] = "estimated"
    result["synced"] = True
    return result


def _fuzz(a: str, b: str) -> float:
    try:
        from rapidfuzz.fuzz import token_set_ratio

        return float(token_set_ratio(a or "", b or ""))
    except Exception:
        return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio() * 100


def _local_lrc(track: Track) -> dict[str, Any] | None:
    p = Path(track.path).with_suffix(".lrc")
    if not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    lines = parse_lrc(raw)
    if lines:
        return _pack("lrc", lines, True)
    plain = _plain_lines(raw)
    if plain:
        return _pack("lrc-plain", plain, False)
    return None


def _from_lrclib_item(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item or not isinstance(item, dict):
        return None
    synced = item.get("syncedLyrics") or ""
    plain = item.get("plainLyrics") or ""
    if synced:
        lines = parse_lrc(synced)
        if lines:
            return _pack("lrclib", lines, True)
    if plain:
        lines = _plain_lines(plain)
        if lines:
            return _pack("lrclib-plain", lines, False)
    return None


async def _lrclib_request(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    label: str,
) -> httpx.Response | None:
    """3 попытки, пауза 2с на 503 / сетевой сбой."""
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            r = await client.get(url, params=params, headers=_UA, timeout=6.0)
            log.info("%s -> %s", label, r.status_code)
            if r.status_code == 404:
                return None
            if r.status_code == 503:
                log.info("%s retry %s/3", label, attempt)
                if attempt < 3:
                    await asyncio.sleep(2)
                continue
            r.raise_for_status()
            return r
        except Exception as exc:
            last = exc
            log.info("%s retry %s/3 (%s)", label, attempt, exc)
            if attempt < 3:
                await asyncio.sleep(2)
    if last:
        log.info("%s fail: %s", label, last)
    return None


async def _lrclib_get(client: httpx.AsyncClient, track: Track) -> dict[str, Any] | None:
    params = {
        "artist_name": track.artist,
        "track_name": track.title,
        "album_name": track.album or "",
        "duration": int(track.duration or 0),
    }
    r = await _lrclib_request(client, LRCLIB_GET, params, "lrclib get")
    if r is None:
        return None
    try:
        return _from_lrclib_item(r.json())
    except Exception as exc:
        log.info("lrclib get fail: %s", exc)
        return None


async def _lrclib_search_pick(client: httpx.AsyncClient, track: Track) -> dict[str, Any] | None:
    r = await _lrclib_request(
        client,
        LRCLIB_SEARCH,
        {"artist_name": track.artist, "track_name": track.title},
        "lrclib search",
    )
    if r is None:
        return None
    try:
        data = r.json()
        items = data if isinstance(data, list) else []
    except Exception as exc:
        log.info("lrclib search fail: %s", exc)
        return None

    want = f"{track.artist} {track.title}"
    ranked: list[tuple[float, dict[str, Any]]] = []
    for it in items[:5]:
        blob = f"{it.get('artistName') or ''} {it.get('trackName') or ''}"
        score = _fuzz(want, blob)
        dur = float(it.get("duration") or 0)
        if track.duration and dur:
            if abs(dur - track.duration) <= 2:
                score += 25
            elif abs(dur - track.duration) <= 8:
                score += 8
            else:
                score -= min(20, abs(dur - track.duration) / 2)
        if it.get("syncedLyrics"):
            score += 6
        ranked.append((score, it))
    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked or ranked[0][0] < 55:
        return None
    return _from_lrclib_item(ranked[0][1])


def _html_to_lines(html: str) -> list[str]:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        chunks: list[str] = []
        for node in soup.select('[data-lyrics-container="true"]'):
            text = node.get_text("\n", strip=True)
            if text:
                chunks.append(text)
        if chunks:
            return "\n".join(chunks).splitlines()
        for sel in (".lyrics", ".lyric-original", "#lyrics-body-text", ".mxm-lyrics"):
            node = soup.select_one(sel)
            if node:
                return node.get_text("\n", strip=True).splitlines()
    except Exception:
        pass
    parts = re.findall(
        r'data-lyrics-container="true"[^>]*>(.*?)</div>',
        html,
        flags=re.S | re.I,
    )
    if parts:
        blob = re.sub(r"<br\s*/?>", "\n", "\n".join(parts), flags=re.I)
        blob = re.sub(r"<[^>]+>", "", blob)
        return [x.strip() for x in blob.splitlines() if x.strip()]
    return []


async def _mirror(client: httpx.AsyncClient, track: Track) -> dict[str, Any] | None:
    """Простые HTML-зеркала без ключей."""
    slug_a = quote(track.artist.replace(" ", "-"))
    slug_t = quote(track.title.replace(" ", "-"))
    urls = [
        f"https://www.musixmatch.com/lyrics/{slug_a}/{slug_t}",
        f"https://teksty-pesenok.ru/search/?q={quote(track.artist + ' ' + track.title)}",
    ]
    for url in urls:
        try:
            r = await client.get(url, headers=_BROWSER, timeout=5.0, follow_redirects=True)
            log.info("lyrics mirror %s -> %s", url[:70], r.status_code)
            if r.status_code >= 400:
                continue
            rows = _html_to_lines(r.text)
            lines = _plain_lines("\n".join(rows))
            if len(lines) >= 4:
                src = "musixmatch" if "musixmatch" in url else "web"
                return _pack(src, lines, False)
        except Exception as exc:
            log.info("mirror fail %s: %s", url[:50], exc)
            continue
    return None


async def _ollama_recall(
    client: httpx.AsyncClient, ollama: OllamaClient, track: Track
) -> dict[str, Any] | None:
    if not ollama.status.models.get(OLLAMA_LLM):
        return None
    prompt = f"""Вспомни текст песни, если уверен.
artist={track.artist!r} title={track.title!r}
Если не уверен или это редкий трек — верни {{"ok": false}}.
Иначе ТОЛЬКО JSON: {{"ok": true, "lyrics": "строка\\nстрока"}}
Без markdown, без комментариев."""
    try:
        raw = await ollama.generate(client, prompt, timeout=OLLAMA_LYRICS_TIMEOUT)
    except Exception as exc:
        log.info("ollama lyrics fail: %s", exc)
        return None
    parsed = extract_json(raw or "")
    if not parsed or not parsed.get("ok"):
        return None
    text = parsed.get("lyrics") or ""
    lines = _plain_lines(str(text))
    if len(lines) < 4:
        return None
    return _pack("ollama", lines, False)


async def fetch_lyrics(
    client: httpx.AsyncClient,
    ollama: OllamaClient,
    track: Track,
    force: bool = False,
) -> dict[str, Any]:
    if not force:
        hit = load_cached(track)
        if hit:
            if hit.get("ok"):
                timed = _estimate_line_timing(hit, track.duration)
                return timed if timed == hit else save_cached(track, timed)
            try:
                age = time.time() - float(hit.get("checked_at", 0))
            except (TypeError, ValueError):
                age = float("inf")
            if 0 <= age < NEGATIVE_CACHE_TTL:
                return hit

    local = _local_lrc(track)
    if local:
        return save_cached(track, _estimate_line_timing(local, track.duration))

    got = await _lrclib_get(client, track)
    if got and got.get("ok"):
        return save_cached(track, _estimate_line_timing(got, track.duration))

    got = await _lrclib_search_pick(client, track)
    if got and got.get("ok"):
        return save_cached(track, _estimate_line_timing(got, track.duration))

    got = await _mirror(client, track)
    if got and got.get("ok"):
        return save_cached(track, _estimate_line_timing(got, track.duration))

    got = await _ollama_recall(client, ollama, track)
    if got and got.get("ok"):
        return save_cached(track, _estimate_line_timing(got, track.duration))

    empty = {
        "ok": False,
        "source": None,
        "synced": False,
        "timing": "none",
        "lines": [],
        "checked_at": time.time(),
    }
    return save_cached(track, empty)

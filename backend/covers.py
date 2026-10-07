"""Поиск обложек: ID3 → Deezer → MusicBrainz/CAA. iTunes выключен (403)."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path
from typing import Any

import httpx

from .config import (
    COVERS_DIR,
    COVERART_ARCHIVE,
    COVER_TIMEOUT,
    DEEZER_SEARCH,
    ITUNES_SEARCH,
    MUSICBRAINZ_RELEASE,
    USER_AGENT,
)

log = logging.getLogger("kadr.covers")

_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
}
_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


def _itunes_hires(url: str) -> str:
    if not url:
        return url
    url = re.sub(r"\d+x\d+bb", "1200x1200bb", url)
    url = url.replace("100x100", "1200x1200").replace("60x60", "1200x1200")
    return url


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def cover_key(artist: str, title: str, album: str) -> str:
    raw = _norm(f"{artist}|{album}|{title}")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _existing_file(track_id: str) -> Path | None:
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    names = [
        f"{track_id}.jpg",
        f"{track_id}.png",
        f"{track_id}.webp",
        f"{track_id}.jpeg",
    ]
    for src in ("embedded", "deezer", "itunes", "coverartarchive", "mb"):
        for ext in (".jpg", ".png", ".jpeg", ".webp"):
            names.append(f"{track_id}_{src}{ext}")
    for name in names:
        p = COVERS_DIR / name
        if p.is_file() and p.stat().st_size > 4000:
            return p
    return None


async def _download(client: httpx.AsyncClient, url: str, dest: Path) -> Path | None:
    try:
        r = await client.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*"},
            timeout=COVER_TIMEOUT,
            follow_redirects=True,
        )
        log.info("cover GET %s -> %s (%s bytes)", url[:80], r.status_code, len(r.content or b""))
        if r.status_code >= 400 or not r.content or len(r.content) < 800:
            if r.status_code >= 400:
                log.info("cover err body %s", (r.text or "")[:180])
            return None
        ctype = r.headers.get("content-type", "")
        if "json" in ctype or "html" in ctype:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest
    except Exception as exc:
        log.info("cover download fail %s: %s", url[:80], exc)
        return None


async def search_deezer(
    client: httpx.AsyncClient, artist: str, title: str, album: str
) -> tuple[str | None, str]:
    last = "-"
    queries = [
        f'artist:"{artist}" track:"{title}"',
        f"{artist} {title}",
        f"{artist} {album}" if album else "",
    ]
    for q in queries:
        if not q:
            continue
        try:
            r = await client.get(
                DEEZER_SEARCH,
                params={"q": q, "limit": 8},
                headers=_BROWSER,
                timeout=COVER_TIMEOUT,
            )
            last = str(r.status_code)
            log.info("deezer %r -> %s", q[:60], r.status_code)
            if r.status_code >= 400:
                log.info("deezer body %s", r.text[:180])
                continue
            results = r.json().get("data") or []
        except Exception as exc:
            last = "err"
            log.info("deezer fail: %s", exc)
            continue
        for item in results:
            alb = item.get("album") or {}
            url = alb.get("cover_xl") or alb.get("cover_big")
            if url:
                return url, last
    return None, last


async def search_itunes(
    client: httpx.AsyncClient, artist: str, title: str, album: str
) -> tuple[str | None, str]:
    last = "-"
    term = f"{artist} {album or title}".strip()
    try:
        r = await client.get(
            ITUNES_SEARCH,
            params={"term": term, "media": "music", "entity": "album", "limit": 8, "country": "RU"},
            headers=_HEADERS,
            timeout=COVER_TIMEOUT,
        )
        last = str(r.status_code)
        log.info("itunes album %r -> %s", term[:60], r.status_code)
        if r.status_code >= 400:
            log.info("itunes body %s", r.text[:180])
            results = []
        else:
            results = r.json().get("results") or []
    except Exception as exc:
        last = "err"
        log.info("itunes fail: %s", exc)
        results = []

    if not results:
        try:
            r = await client.get(
                ITUNES_SEARCH,
                params={"term": f"{artist} {title}", "media": "music", "limit": 8, "country": "RU"},
                headers=_HEADERS,
                timeout=COVER_TIMEOUT,
            )
            last = str(r.status_code)
            log.info("itunes song %r -> %s", f"{artist} {title}"[:60], r.status_code)
            if r.status_code < 400:
                results = r.json().get("results") or []
        except Exception as exc:
            last = "err"
            log.info("itunes song fail: %s", exc)
            results = []

    best = None
    an, tn, aln = _norm(artist), _norm(title), _norm(album)
    for item in results:
        art = item.get("artworkUrl100") or item.get("artworkUrl60")
        if not art:
            continue
        ia = _norm(item.get("artistName") or "")
        it = _norm(item.get("trackName") or "")
        ial = _norm(item.get("collectionName") or "")
        if an and an in ia and (tn in it or tn in ial or aln in ial or not tn):
            return _itunes_hires(art), last
        if best is None:
            best = art
    return (_itunes_hires(best) if best else None), last


async def search_musicbrainz_cover(
    client: httpx.AsyncClient, artist: str, title: str, album: str
) -> tuple[str | None, str]:
    """Всегда (url|None, статус). 503 → skip, без распаковки None."""
    query = f'artist:"{artist}" AND release:"{album or title}"'
    try:
        r = await client.get(
            MUSICBRAINZ_RELEASE,
            params={"query": query, "fmt": "json", "limit": 3},
            headers={**_HEADERS, "User-Agent": USER_AGENT},
            timeout=COVER_TIMEOUT,
        )
    except Exception as exc:
        log.info("musicbrainz fail: %s", exc)
        return None, "err"
    code = str(r.status_code)
    log.info("musicbrainz %r -> %s", query[:70], r.status_code)
    if r.status_code == 503:
        log.info("musicbrainz busy, skip")
        return None, "skip"
    if r.status_code != 200:
        log.info("musicbrainz body %s", (r.text or "")[:180])
        return None, code
    try:
        releases = (r.json() or {}).get("releases") or []
    except Exception:
        return None, "skip"
    if not isinstance(releases, list):
        return None, "skip"

    for rel in releases:
        if not isinstance(rel, dict):
            continue
        mbid = rel.get("id")
        if not mbid:
            continue
        url = f"{COVERART_ARCHIVE}/{mbid}/front-1200"
        try:
            head = await client.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*"},
                timeout=COVER_TIMEOUT,
                follow_redirects=True,
            )
            log.info("caa %s -> %s", mbid, head.status_code)
            if head.status_code == 503:
                return None, "skip"
            if head.status_code == 200 and head.content and len(head.content) > 800:
                ctype = head.headers.get("content-type", "")
                if "image" in ctype or not ctype:
                    return str(head.url), "200"
        except Exception as exc:
            log.info("caa fail %s: %s", mbid, exc)
            continue
    return None, code


def _from_embedded(track_id: str, embedded: str | None) -> dict[str, Any] | None:
    if not embedded:
        return None
    src = Path(embedded)
    if not src.is_file() or src.stat().st_size < 800:
        return None
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower() if src.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"
    dest = COVERS_DIR / f"{track_id}{ext}"
    try:
        if dest.resolve() != src.resolve():
            shutil.copyfile(src, dest)
        else:
            dest = src
    except OSError:
        dest = src
    return {
        "source": "id3",
        "original_url": None,
        "file": dest.name,
        "path": str(dest),
    }


async def fetch_cover(
    client: httpx.AsyncClient,
    track_id: str,
    artist: str,
    title: str,
    album: str,
    embedded: str | None = None,
) -> dict[str, Any] | None:
    hit, _ = await fetch_cover_report(client, track_id, artist, title, album, embedded)
    return hit


async def fetch_cover_report(
    client: httpx.AsyncClient,
    track_id: str,
    artist: str,
    title: str,
    album: str,
    embedded: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """Каскад + статусы источников для лога cover-find."""
    report = {"deezer": "-", "itunes": "skip", "mb": "-"}
    COVERS_DIR.mkdir(parents=True, exist_ok=True)

    cached = _existing_file(track_id)
    if cached:
        src = "id3" if "embedded" in cached.name else "cache"
        return (
            {"source": src, "original_url": None, "file": cached.name, "path": str(cached)},
            {"deezer": "cache", "itunes": "skip", "mb": "cache"},
        )

    hit = _from_embedded(track_id, embedded)
    if hit:
        log.info("cover id3 %s", track_id)
        return hit, {"deezer": "id3", "itunes": "skip", "mb": "id3"}

    # iTunes 403 — выключен. Deezer первым, затем MusicBrainz/CAA.
    sources = [
        ("deezer", search_deezer),
        ("mb", search_musicbrainz_cover),
    ]
    for name, fn in sources:
        url = None
        code = "-"
        try:
            got = await fn(client, artist, title, album)
            if got is None:
                url, code = None, "skip"
            elif isinstance(got, tuple):
                url = got[0] if got else None
                code = str(got[1]) if len(got) > 1 else "-"
            else:
                url, code = got, "200"
        except Exception as ext:
            log.info("cover source %s exc: %s", name, ext)
            url, code = None, "err"
        if code in {"503", "429"}:
            code = "skip"
        report[name] = code
        if not url:
            continue
        dest = COVERS_DIR / f"{track_id}.jpg"
        saved = await _download(client, url, dest)
        if saved:
            return (
                {
                    "source": name,
                    "original_url": url,
                    "file": saved.name,
                    "path": str(saved),
                },
                report,
            )
    return None, report


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _host_ok(url: str) -> bool:
    from urllib.parse import urlparse

    h = (urlparse(url).hostname or "").lower()
    if not h:
        return False
    if h.endswith(".mzstatic.com") or h in {"is1-ssl.mzstatic.com"}:
        return True
    if h.endswith(".dzcdn.net"):
        return True
    if h.endswith(".musicbrainz.org") or h == "coverartarchive.org":
        return True
    if h.endswith(".ytimg.com") or h in {"i.ytimg.com", "img.youtube.com"}:
        return True
    if h.endswith(".googleusercontent.com"):
        return True
    return False


async def proxy_cover(
    client: httpx.AsyncClient, url: str
) -> tuple[bytes, str] | None:
    """Скачать картинку (кэш sha256), None если не вышло."""
    if not url.startswith(("http://", "https://")):
        return None
    if not _host_ok(url):
        log.info("cover-proxy host denied %s", url[:80])
        return None
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    for ext in (".jpg", ".png", ".webp", ".jpeg"):
        p = COVERS_DIR / f"px_{key}{ext}"
        if p.is_file() and p.stat().st_size > 400:
            mime = "image/png" if ext == ".png" else "image/webp" if ext == ".webp" else "image/jpeg"
            return p.read_bytes(), mime
    try:
        r = await client.get(
            url,
            headers={"User-Agent": _BROWSER_UA, "Accept": "image/avif,image/webp,image/*,*/*"},
            timeout=8.0,
            follow_redirects=True,
        )
    except Exception as exc:
        log.info("cover-proxy fail %s: %s", url[:80], exc)
        return None
    if r.status_code >= 400 or not r.content or len(r.content) < 200:
        log.info("cover-proxy %s -> %s", url[:80], r.status_code)
        return None
    ctype = (r.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if "html" in ctype or "json" in ctype:
        return None
    ext = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".jpg"
    dest = COVERS_DIR / f"px_{key}{ext}"
    try:
        dest.write_bytes(r.content)
    except OSError:
        pass
    return r.content, ctype if ctype.startswith("image/") else "image/jpeg"

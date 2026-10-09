"""
Курымдык — локальный медиаплеер.
Backend: FastAPI. Все ИИ-запросы только на http://127.0.0.1:11434 (Ollama).
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.requests import ClientDisconnect, Request
from fastapi.staticfiles import StaticFiles

from .artists import build_artists
from .artist_discography import fetch_artist_releases
from .cache_store import CacheStore
from .config import (
    CACHE_DIR,
    COVERS_DIR,
    APP_SETTINGS_DB_PATH,
    DEFAULT_MUSIC_FOLDER,
    EMBEDDED_DIR,
    FRONTEND_DIR,
    HOST,
    OLLAMA_URL,
    PORT,
    ROOT,
    USER_AGENT,
)
from .error_logging import configure_error_logging
from .censorship_detector import detect_suspects
from .covers import fetch_cover_report, proxy_cover
from .index_store import IndexStore
from .audio_fetcher import AudioFetcher
from .lastfm_client import LastFmClient, LastFmConfigurationError, LastFmError
from .lyrics import fetch_lyrics, lyrics_cache_path
from .media_resolver import MediaResolver
from .ollama_ai import OllamaClient
from .placeholder import generate_placeholder
from .scanner import Track, read_track, scan_folder
from .translator import translate_biography
from . import stats as listening_stats
from .uncensored_finder import find_candidates
from .uncensored_replacer import replace_track
from . import dj as dj_engine
from . import network_router
from . import wave as wave_engine
from . import weekly as weekly_engine

# Windows-консоль: нормальный UTF-8 для кириллицы в логах
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

CACHE_DIR.mkdir(parents=True, exist_ok=True)
COVERS_DIR.mkdir(parents=True, exist_ok=True)
EMBEDDED_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("OLLAMA_NUM_GPU", "1")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
configure_error_logging(CACHE_DIR / "errors.log")
logging.getLogger("kadr").setLevel(logging.INFO)
logging.getLogger("kadr.covers").setLevel(logging.INFO)
logging.getLogger("kadr.lyrics").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class _QuietDisconnect(logging.Filter):
    """Глушим WinError 10054 / ConnectionReset — браузер рвёт Range-стрим."""

    _NEEDLES = (
        "10054",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "_call_connection_lost",
        "Connection lost",
        "WinError",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        if any(n in msg for n in self._NEEDLES):
            return False
        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(
            exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ConnectionError)
        ):
            return False
        return True


def _silence_disconnect(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    exc = context.get("exception")
    if isinstance(
        exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ConnectionError)
    ):
        return
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 10054:
        return
    msg = str(context.get("message") or "")
    if "10054" in msg or "_call_connection_lost" in msg or "Connection lost" in msg:
        return
    loop.default_exception_handler(context)


_quiet = _QuietDisconnect()
for _name in ("asyncio", "uvicorn", "uvicorn.error", "uvicorn.access", "starlette"):
    logging.getLogger(_name).addFilter(_quiet)

app = FastAPI(title="Курымдык", version="1.0.0", docs_url="/api/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(sqlite3.OperationalError)
async def _sqlite_error(_request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
    logging.getLogger("kadr.errors").exception("SQLite operation failed")
    if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
        return JSONResponse(
            status_code=503,
            content={"detail": "База данных занята, повторите запрос"},
        )
    return JSONResponse(
        status_code=500,
        content={"detail": "Не удалось выполнить операцию с базой данных"},
    )


@app.exception_handler(Exception)
async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logging.getLogger("kadr.errors").exception(
        "Unhandled error for %s %s", request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Внутренняя ошибка сервера; подробности в cache/errors.log"},
    )


cache = CacheStore()
track_index = IndexStore()
ollama = OllamaClient(OLLAMA_URL)
resolver = MediaResolver(cache, ollama, track_index)
lastfm_client = LastFmClient()
audio_fetcher = AudioFetcher()
download_tasks: dict[str, dict[str, Any]] = {}

# Состояние библиотеки в памяти
library: dict[str, Track] = {}
current_folder: str | None = None


def _saved_library_folder() -> str | None:
    # FIX Bug5: persist music folder with unicode (Cyrillic) correctly — uses utf-8 TEXT
    if not APP_SETTINGS_DB_PATH.is_file():
        return None
    try:
        with sqlite3.connect(APP_SETTINGS_DB_PATH) as connection:
            # ensure utf-8 handling for Cyrillic paths like C:\\Users\\user\\Music\\Музыка
            connection.text_factory = str
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'library_folder'"
            ).fetchone()
        return str(row[0]) if row and row[0] else None
    except sqlite3.Error:
        logging.getLogger("kadr.errors").exception("Could not read saved music folder")
        return None


def _save_library_folder(folder: str) -> None:
    # FIX Bug5: save unicode path to SQLite + ensure directory exists; called on scan and persists across restarts
    try:
        # normalize unicode path — keep Cyrillic like Музыка intact
        folder = str(Path(folder).expanduser())
        APP_SETTINGS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(APP_SETTINGS_DB_PATH) as connection:
            connection.text_factory = str
            connection.execute(
                "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO app_settings (key, value) VALUES ('library_folder', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (folder,),
            )
    except sqlite3.Error:
        logging.getLogger("kadr.errors").exception("Could not save music folder")
resolve_task: asyncio.Task[Any] | None = None

http_client: httpx.AsyncClient | None = None
uncensored_scan_task: asyncio.Task[None] | None = None
uncensored_scan_state: dict[str, Any] = {
    "status": "idle",
    "processed": 0,
    "total": 0,
    "suspects": 0,
    "items": [],
    "by_reason": {},
    "error": None,
}
uncensored_replace_task: asyncio.Task[None] | None = None
uncensored_replace_state: dict[str, Any] = {
    "status": "idle",
    "processed": 0,
    "total": 0,
    "results": [],
    "error": None,
}
wave_embedding_task: asyncio.Task[int] | None = None
wave_embedding_maintenance_task: asyncio.Task[None] | None = None
network_check_task: asyncio.Task[None] | None = None
wave_embedding_state: dict[str, Any] = {
    "status": "idle",
    "embedded": 0,
    "total": 0,
    "error": None,
}

_uncensored_log = logging.getLogger("kadr.uncensored")
_uncensored_log.setLevel(logging.INFO)
_uncensored_log.propagate = True
if not any(
    isinstance(handler, logging.FileHandler)
    and Path(handler.baseFilename).name == "uncensored_replacements.log"
    for handler in _uncensored_log.handlers
):
    (Path(__file__).resolve().parent.parent / ".tools").mkdir(
        parents=True, exist_ok=True
    )
    _uncensored_file_handler = logging.FileHandler(
        Path(__file__).resolve().parent.parent / ".tools" / "uncensored_replacements.log",
        encoding="utf-8",
    )
    _uncensored_file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    _uncensored_log.addHandler(_uncensored_file_handler)


class Hub:
    """Фан-аут событий резолвера в браузер по WebSocket."""

    def __init__(self) -> None:
        self.clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.clients:
            self.clients.remove(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


hub = Hub()


@app.exception_handler(ClientDisconnect)
async def _client_gone(_request, _exc) -> Response:
    return Response(status_code=204)


@app.on_event("startup")
async def _startup() -> None:
    global http_client
    try:
        asyncio.get_running_loop().set_exception_handler(_silence_disconnect)
    except Exception:
        pass
    http_client = httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        timeout=30.0,
    )
    lastfm_client.client = http_client
    await ollama.refresh_status(http_client)
    # start VPN network checks each 5min
    global network_check_task
    try:
        network_check_task = asyncio.create_task(network_router.background_loop(http_client))
    except Exception:
        pass
    st = ollama.status.to_dict()
    print("=== Курымдык ===")
    print(f"UI:     http://{HOST}:{PORT}")
    print(f"Ollama: {st['url']}  online={st['online']}  models={st['models']}")


@app.on_event("shutdown")
async def _shutdown() -> None:
    global wave_embedding_task, wave_embedding_maintenance_task
    tasks = [
        task
        for task in (wave_embedding_task, wave_embedding_maintenance_task, network_check_task)
        if task and not task.done()
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if http_client:
        await http_client.aclose()
    lastfm_client.client = None


def _client() -> httpx.AsyncClient:
    if http_client is None:
        raise HTTPException(503, "HTTP-клиент ещё не готов")
    return http_client


def _track_public(t: Track) -> dict[str, Any]:
    media = cache.get(t.id) or {}
    return {
        **t.to_dict(),
        "media": {
            "display": media.get("display"),
            "youtube_id": media.get("youtube_id"),
            "cover_file": media.get("cover_file"),
            "cover_source": media.get("cover_source"),
            "confidence": media.get("confidence"),
            "reason": media.get("reason"),
        } if media else None,
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.post("/api/shutdown")
async def api_shutdown() -> dict[str, bool]:
    """Мягкая остановка uvicorn (из трея)."""
    import asyncio
    import os
    import signal

    async def _die() -> None:
        await asyncio.sleep(0.25)
        try:
            os.kill(os.getpid(), signal.SIGINT)
        except Exception:
            os._exit(0)

    asyncio.create_task(_die())
    return {"ok": True}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    await ollama.refresh_status(_client())
    return {
        "ok": True,
        "folder": current_folder or _saved_library_folder() or (
            str(DEFAULT_MUSIC_FOLDER) if DEFAULT_MUSIC_FOLDER.is_dir() else None
        ),
        "tracks": len(library),
        "ollama": ollama.status.to_dict(),
    }


@app.get("/api/artists")
async def api_artists() -> dict[str, Any]:
    """Уникальные исполнители + похожие (nomic-embed-text, если Ollama жива)."""
    tracks = list(library.values())
    return await build_artists(_client(), ollama, tracks)


@app.get("/api/lastfm/similar/{artist}")
async def api_lastfm_similar(
    artist: str, limit: int = Query(default=20, ge=1, le=100)
) -> dict[str, Any]:
    try:
        similar = await lastfm_client.get_similar_artists(artist, limit)
    except LastFmConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    except LastFmError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"artist": artist, "similar": similar}


@app.get("/api/lastfm/top/{artist}")
async def api_lastfm_top(
    artist: str, limit: int = Query(default=10, ge=1, le=100)
) -> dict[str, Any]:
    try:
        tracks = await lastfm_client.get_artist_top_tracks(artist, limit)
    except LastFmConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    except LastFmError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"artist": artist, "tracks": tracks}


@app.get("/api/lastfm/info/{artist}")
async def api_lastfm_info(artist: str) -> dict[str, Any]:
    try:
        info = await lastfm_client.get_artist_info(artist)
    except LastFmConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    except LastFmError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"artist": info}


@app.get("/api/artist/{name:path}")
async def api_artist_profile(name: str) -> dict[str, Any]:
    name = name.strip()
    if not name:
        raise HTTPException(400, "Укажите имя исполнителя")
    warnings: list[str] = []
    try:
        info = await lastfm_client.get_artist_info(name)
        artist_name = str(info.get("name") or name).strip()
        similar, top_tracks, tags = await asyncio.gather(
            lastfm_client.get_similar_artists(artist_name, 10),
            lastfm_client.get_artist_top_tracks(artist_name, 20),
            lastfm_client.get_artist_top_tags(artist_name, 10),
        )
    except LastFmError as exc:
        logging.getLogger("kadr.artist").warning(
            "Last.fm profile lookup failed for %s: %s", name, type(exc).__name__
        )
        info = {}
        artist_name = name
        similar, top_tracks, tags = [], [], []
        warnings.append("Данные об артисте недоступны. Проверьте VPN или интернет")

    artist_keys = {artist_name.casefold(), name.casefold()}
    local_tracks = [
        track.to_dict()
        for track in library.values()
        if track.artist.strip().casefold() in artist_keys
    ]
    formatted_tracks = []
    for track in top_tracks:
        title = str(track.get("name") or "").strip()
        if not title:
            continue
        track_artist = track.get("artist")
        formatted_tracks.append(
            {
                "title": title,
                "url": str(track.get("url") or ""),
                "artist": str(track_artist.get("name") or artist_name)
                if isinstance(track_artist, dict)
                else artist_name,
                "listeners": str(track.get("listeners") or ""),
                "in_library": any(
                    local["title"].strip().casefold() == title.casefold()
                    for local in local_tracks
                ),
            }
        )
    top_tracks = formatted_tracks
    normalized_tags = []
    for tag in tags:
        tag_name = str(tag.get("name") or "").strip()
        if not tag_name:
            continue
        try:
            count = int(tag.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        normalized_tags.append({"name": tag_name, "count": count})

    releases: list[dict[str, Any]] = []
    release_error = None
    mbid = str(info.get("mbid") or "")
    if mbid:
        try:
            releases = await fetch_artist_releases(_client(), mbid, months=3)
        except (httpx.HTTPError, ValueError) as exc:
            logging.getLogger("kadr.musicbrainz").exception(
                "MusicBrainz profile lookup failed for %s", artist_name
            )
            release_error = f"MusicBrainz недоступен: {type(exc).__name__}"
    else:
        release_error = "Для исполнителя нет MusicBrainz ID"

    bio_data = info.get("bio")
    bio = str(bio_data.get("summary") or "") if isinstance(bio_data, dict) else ""
    bio = html.unescape(re.sub(r"<[^>]*>", " ", bio))
    bio = " ".join(bio.split())
    if bio:
        bio = await translate_biography(bio, _client(), ollama)
    images = info.get("image") or []
    image_url = ""
    if isinstance(images, list):
        image_url = next(
            (
                str(image.get("#text") or "")
                for image in reversed(images)
                if isinstance(image, dict) and image.get("#text")
            ),
            "",
        )
        if image_url and not image_url.startswith("https://lastfm.freetls.fastly.net/"):
            image_url = ""
    if not image_url:
        try:
            response = await _client().get(
                "https://api.deezer.com/search/artist",
                params={"q": artist_name},
                timeout=5.0,
            )
            response.raise_for_status()
            payload = response.json()
            candidates = payload.get("data", []) if isinstance(payload, dict) else []
            if not isinstance(candidates, list):
                candidates = []
            match = next(
                (
                    item
                    for item in candidates
                    if isinstance(item, dict)
                    and str(item.get("name") or "").strip().casefold()
                    == artist_name.casefold()
                ),
                None,
            )
            if match:
                candidate = str(
                    match.get("picture_xl")
                    or match.get("picture_big")
                    or match.get("picture_medium")
                    or ""
                )
                if candidate.startswith("https://"):
                    parsed_image = httpx.URL(candidate)
                    if (parsed_image.host or "").casefold().endswith(".dzcdn.net"):
                        image_url = candidate
        except (httpx.HTTPError, ValueError) as exc:
            logging.getLogger("kadr.artist").warning(
                "Deezer artist image lookup failed for %s: %s",
                artist_name,
                type(exc).__name__,
            )

    return {
        "artist": artist_name,
        "image": image_url,
        "bio": bio,
        "genres": normalized_tags,
        "similar": [
            {"name": str(item.get("name") or ""), "url": str(item.get("url") or "")}
            for item in similar
            if item.get("name")
        ],
        "local_tracks": local_tracks,
        "top_tracks": top_tracks,
        "releases": releases,
        "warnings": warnings + ([release_error] if release_error else []),
        "lastfm_available": not bool(warnings),
        "sources": {"lastfm": "Last.fm", "musicbrainz": "MusicBrainz"},
    }


@app.post("/api/stats/play")
async def api_record_play(body: dict[str, Any]) -> dict[str, Any]:
    track_id = str(body.get("track_id") or "").strip()
    track = library.get(track_id)
    if track is None:
        raise HTTPException(404, "Трек не найден в текущей библиотеке")
    try:
        duration = float(body.get("duration"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Некорректная длительность прослушивания")
    if not 30 < duration <= 24 * 60 * 60:
        raise HTTPException(400, "Прослушивание должно быть больше 30 секунд")
    session_id = str(body.get("session_id") or "").strip()
    if not 1 <= len(session_id) <= 100:
        raise HTTPException(400, "Некорректный session_id")
    try:
        play = listening_stats.record_play(
            track.id,
            session_id,
            duration,
            track.artist,
            track.title,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "play": play}


@app.get("/api/stats/summary")
async def api_stats_summary() -> dict[str, Any]:
    return listening_stats.summary()


@app.get("/api/stats/top")
async def api_stats_top(
    period: str = Query(default="all", pattern="^(day|week|month|all)$")
) -> dict[str, Any]:
    return listening_stats.top(period)


@app.get("/api/stats/timeline")
async def api_stats_timeline(
    days: int = Query(default=30, ge=1, le=365)
) -> dict[str, Any]:
    return listening_stats.timeline(days)


@app.get("/api/stats/genres")
async def api_stats_genres() -> dict[str, Any]:
    artists = listening_stats.genre_artists()
    if not artists:
        return {"genres": [], "warnings": ["Недостаточно истории прослушиваний"]}
    totals: dict[str, float] = {}
    genre_artists_map: dict[str, set[str]] = {}
    warnings = []
    for item in artists:
        try:
            tags = await lastfm_client.get_artist_top_tags(item["artist"], 10)
        except LastFmConfigurationError:
            warnings.append("Для жанров Last.fm задайте LASTFM_API_KEY")
            break
        except LastFmError as exc:
            logging.getLogger("kadr.stats").warning(
                "Last.fm tags unavailable for %s: %s", item["artist"], exc
            )
            continue
        for tag in tags:
            name = str(tag.get("name") or "").strip()
            if not name:
                continue
            try:
                tag_weight = max(1, int(tag.get("count") or 1))
            except (TypeError, ValueError):
                tag_weight = 1
            weight = item["seconds"] * tag_weight
            totals[name] = totals.get(name, 0.0) + weight
            genre_artists_map.setdefault(name, set()).add(item["artist"])
    genres = [
        {"name": name, "weight": round(weight, 2), "artists": len(genre_artists_map[name])}
        for name, weight in sorted(totals.items(), key=lambda entry: entry[1], reverse=True)[:12]
    ]
    return {"genres": genres, "warnings": warnings}


@app.get("/api/stats/export.csv")
async def api_stats_export_csv() -> Response:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["track_id", "timestamp_utc", "duration_seconds", "artist", "title"])
    for play in listening_stats.csv_rows():
        values = [
            play["track_id"],
            play["timestamp"],
            play["duration"],
            play["artist"],
            play["title"],
        ]
        writer.writerow(
            [
                "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t")) else value
                for value in values
            ]
        )
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="kadr-listening-stats.csv"'},
    )


async def _daily_wave_embedding_refresh() -> None:
    while True:
        await asyncio.sleep(24 * 60 * 60)
        try:
            if not library:
                continue
            if wave_embedding_task and not wave_embedding_task.done():
                await wave_embedding_task
            await _start_wave_embedding_warmup()
            if wave_embedding_task:
                await wave_embedding_task
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger("kadr.wave").exception(
                "Daily embedding refresh failed; will retry tomorrow"
            )


async def _start_wave_embedding_warmup() -> None:
    global wave_embedding_task, wave_embedding_maintenance_task
    if not library:
        raise HTTPException(400, "Сначала выберите папку с музыкой")
    if (
        wave_embedding_maintenance_task is None
        or wave_embedding_maintenance_task.done()
    ):
        wave_embedding_maintenance_task = asyncio.create_task(
            _daily_wave_embedding_refresh()
        )
    existing = wave_engine.load_embeddings()
    matching = sum(1 for track_id in library if track_id in existing)
    missing = len(library) - matching
    if missing <= 0:
        wave_embedding_state.update(
            status="ready", embedded=matching, total=len(library), error=None
        )
        return
    if wave_embedding_task and not wave_embedding_task.done():
        return

    wave_embedding_state.update(
        status="running", embedded=matching, total=len(library), error=None
    )

    async def worker() -> int:
        try:
            count = await wave_engine.ensure_embeddings(
                list(library.values()), _client(), ollama
            )
            current_embeddings = wave_engine.load_embeddings()
            current = sum(1 for track_id in library if track_id in current_embeddings)
            wave_embedding_state.update(
                status="ready" if current >= len(library) else "partial",
                embedded=current,
                total=len(library),
                error=None if current >= len(library) else "Embedding model unavailable or failed",
            )
            return count
        except Exception as exc:
            logging.getLogger("kadr.wave").exception("Embedding warmup failed")
            wave_embedding_state.update(
                status="error",
                embedded=len(wave_engine.load_embeddings()),
                total=len(library),
                error=f"{type(exc).__name__}: {exc}",
            )
            return 0

    wave_embedding_task = asyncio.create_task(worker())


async def _wave_queue_result(
    count: int,
    exclude: str,
    mode: str = "new",
    mood: str | None = None,
    language: str | None = None,
    balance: int = 40,
) -> dict[str, Any]:
    if not library:
        raise HTTPException(400, "Сначала выберите папку с музыкой")
    if mode not in wave_engine.WAVE_MODES:
        raise HTTPException(400, "Неизвестный режим волны")
    if mood is not None and mood not in wave_engine.WAVE_MOODS:
        raise HTTPException(400, "Неизвестный фильтр настроения")
    if language is not None and language not in wave_engine.WAVE_LANGUAGES:
        raise HTTPException(400, "Неизвестный фильтр языка")
    await _start_wave_embedding_warmup()
    excluded = {track_id for track_id in exclude.split(",") if track_id}
    result = await wave_engine.recommend(
        list(library.values()),
        count=count,
        exclude_ids=excluded,
        client=_client(),
        ollama=ollama,
        lastfm=lastfm_client,
        mode=mode,
        mood_filter=mood,
        language_filter=language,
        balance=balance,
    )
    result["embedding_status"] = dict(wave_embedding_state)
    return result


@app.get("/api/wave/queue")
async def api_wave_queue(
    count: int = Query(default=10, ge=1, le=50),
    exclude: str = Query(default="", max_length=4000),
    mode: str = Query(default="new"),
    mood: str | None = Query(default=None),
    language: str | None = Query(default=None),
    balance: int = Query(default=40, ge=0, le=100),
) -> dict[str, Any]:
    return await _wave_queue_result(count, exclude, mode, mood, language, balance)


@app.get("/api/wave/likes")
async def api_wave_likes() -> dict[str, list[str]]:
    return {"track_ids": wave_engine.liked_track_ids()}


@app.get("/api/wave/next")
async def api_wave_next(
    mode: str = Query(default="new"),
    mood: str | None = Query(default=None),
    language: str | None = Query(default=None),
    balance: int = Query(default=40, ge=0, le=100),
) -> dict[str, Any]:
    result = await _wave_queue_result(1, "", mode, mood, language, balance)
    items = result.pop("items")
    if not items:
        raise HTTPException(404, result.get("warning") or "В библиотеке нет доступных треков для волны")
    result["track"] = items[0]
    return result


@app.post("/api/wave/signal")
async def api_wave_signal(body: dict[str, Any]) -> dict[str, Any]:
    track_id = str(body.get("track_id") or "").strip()
    if track_id not in library:
        raise HTTPException(404, "Трек не найден в текущей библиотеке")
    signal = str(body.get("signal") or "").strip()
    if signal not in wave_engine.SIGNAL_WEIGHTS:
        raise HTTPException(400, "Допустимые реакции: skip, complete, like, dislike")
    if signal == "skip":
        try:
            duration = float(body.get("duration", 0))
        except (TypeError, ValueError):
            raise HTTPException(400, "Некорректная длительность до пропуска")
        if not 0 <= duration < 30:
            raise HTTPException(400, "Пропуск учитывается только до 30 секунд")
    track = library[track_id]
    return wave_engine.record_signal(
        track_id,
        signal,
        artist=track.artist,
        genre=str(getattr(track, "genre", "") or ""),
    )


# ---------- Download API (Task1) ----------
import uuid as _uuid

def _sanitize_fs(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:100] if len(name) > 100 else name) or "Unknown"

def _download_target_path(artist: str, title: str, album: str, year: str, track_no: str, library_folder: str | None) -> Path:
    base = Path(library_folder or current_folder or str(DEFAULT_MUSIC_FOLDER))
    artist_dir = _sanitize_fs(artist or "Unknown Artist")
    album_dir = f"{_sanitize_fs(year) + ' - ' if year else ''}{_sanitize_fs(album or 'Singles')}"
    filename = f"{_sanitize_fs(track_no).zfill(2) + ' - ' if track_no else ''}{_sanitize_fs(title or 'Unknown Title')}"
    return base / artist_dir / album_dir / filename

@app.post("/api/download")
async def api_download(body: dict[str, Any]) -> dict[str, Any]:
    url = str(body.get("url") or "").strip()
    artist = str(body.get("artist") or "").strip()
    title = str(body.get("title") or "").strip()
    album = str(body.get("album") or "").strip()
    year = str(body.get("year") or "").strip()
    track_no = str(body.get("track_no") or body.get("track") or "").strip()
    try:
        preview_duration = float(body.get("preview_duration") or body.get("duration") or 0) or None
    except Exception:
        preview_duration = None
    if not url or not artist or not title:
        raise HTTPException(400, "Нужно url, artist, title")
    if not url.startswith("https://"):
        raise HTTPException(400, "URL должен быть https")
    # Validate host for safety
    try:
        parsed = httpx.URL(url)
        if parsed.scheme != "https":
            raise ValueError()
    except Exception:
        raise HTTPException(400, "Некорректный URL")
    target = _download_target_path(artist, title, album, year, track_no, current_folder)
    final = target.with_suffix(".opus")
    if final.is_file():
        return {"status": "exists", "path": str(final)}
    task_id = _uuid.uuid4().hex[:12]
    download_tasks[task_id] = {"status": "queued", "progress": 0, "url": url, "artist": artist, "title": title}
    async def _run():
        try:
            download_tasks[task_id]["status"] = "downloading"
            download_tasks[task_id]["progress"] = 10
            result = audio_fetcher.fetch_with_tags(url, target, artist, title, album, year, track_no, preview_duration=preview_duration)
            if result.get("status") == "exists":
                download_tasks[task_id].update(status="exists", path=result.get("path"), progress=100)
                return
            if result.get("status") != "ok":
                download_tasks[task_id].update(status="error", detail=result.get("detail"), progress=100)
                return
            path = result.get("path")
            download_tasks[task_id].update(status="done", path=path, progress=100)
            # Update library
            try:
                from .scanner import read_track
                tr = read_track(Path(path))
                if tr:
                    library[tr.id] = tr
                    track_index.upsert(tr.id, path=tr.path, artist=tr.artist, title=tr.title, album=tr.album, duration=tr.duration)
            except Exception as exc:
                logging.getLogger("kadr.download").warning("Library update failed for %s: %s", path, exc)
        except Exception as exc:
            logging.getLogger("kadr.download").exception("Download task failed")
            download_tasks[task_id].update(status="error", detail=str(exc), progress=100)
    asyncio.create_task(_run())
    return {"status": "queued", "task_id": task_id, "path": str(final)}

@app.post("/api/download/by-search")
async def api_download_by_search(body: dict[str, Any]) -> dict[str, Any]:
    artist = str(body.get("artist") or "").strip()
    title = str(body.get("title") or "").strip()
    album = str(body.get("album") or "").strip()
    year = str(body.get("year") or "").strip()
    if not artist or not title:
        raise HTTPException(400, "Нужно artist и title")
    url = audio_fetcher.search_url(artist, title)
    if not url:
        raise HTTPException(404, "Не удалось найти URL для скачивания")
    return await api_download({"url": url, "artist": artist, "title": title, "album": album, "year": year})

@app.get("/api/download/status/{task_id}")
async def api_download_status(task_id: str) -> dict[str, Any]:
    task = download_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    return task

@app.post("/api/new-releases")
async def api_new_releases(body: dict[str, Any]) -> dict[str, Any]:
    artists = body.get("artists", [])
    if not isinstance(artists, list):
        artists = []
    return await _weekly_snapshot(
        force=bool(body.get("force")),
        followed_artists=[str(name) for name in artists],
    )


async def _weekly_snapshot(
    force: bool = False,
    followed_artists: list[str] | None = None,
) -> dict[str, Any]:
    if not library:
        raise HTTPException(400, "Сначала выберите папку с музыкой")
    return await weekly_engine.get_weekly(
        _client(),
        lastfm_client,
        list(library.values()),
        ollama=ollama,
        force=force,
        extra_artists=followed_artists,
    )


@app.get("/api/weekly/tracks")
async def api_weekly_tracks() -> dict[str, Any]:
    data = await _weekly_snapshot()
    return {key: value for key, value in data.items() if key != "albums"}


@app.get("/api/weekly/albums")
async def api_weekly_albums() -> dict[str, Any]:
    data = await _weekly_snapshot()
    return {key: value for key, value in data.items() if key != "tracks"}


@app.get("/api/weekly")
async def api_weekly(artist: list[str] = Query(default=[])) -> dict[str, Any]:
    return await _weekly_snapshot(followed_artists=artist)


@app.post("/api/weekly/refresh")
async def api_weekly_refresh(body: dict[str, Any] | None = None) -> dict[str, Any]:
    artists = (body or {}).get("artists", [])
    if not isinstance(artists, list):
        artists = []
    return await _weekly_snapshot(
        force=True,
        followed_artists=[str(name) for name in artists],
    )


@app.get("/api/library")
async def get_library() -> dict[str, Any]:
    tracks = [_track_public(t) for t in library.values()]
    # Сохраняем порядок сканирования (альбом / номер)
    return {"folder": current_folder, "count": len(tracks), "tracks": tracks}


@app.post("/api/scan")
async def scan(body: dict[str, Any]) -> dict[str, Any]:
    """Сканируем папку с музыкой. Резолв клипов — ленивый, по запросу."""
    global library, current_folder
    folder_value = (body or {}).get("path") or ""
    folder_value = str(folder_value).strip().strip('"')
    if not folder_value:
        raise HTTPException(400, "Укажите путь к папке")
    p = Path(folder_value).expanduser()
    if not p.exists() or not p.is_dir():
        raise HTTPException(400, f"Папка не найдена: {folder_value}")

    resolved_folder = str(p.resolve())
    tracks = scan_folder(resolved_folder)
    library = {t.id: t for t in tracks}
    current_folder = resolved_folder
    _save_library_folder(current_folder)
    return {
        "folder": current_folder,
        "count": len(tracks),
        "tracks": [_track_public(t) for t in tracks],
    }


def _uncensored_scan_snapshot() -> dict[str, Any]:
    return {**uncensored_scan_state}


@app.get("/api/uncensored/scan")
async def uncensored_scan() -> dict[str, Any]:
    """Start a Last.fm-backed scan, or return its current result."""
    global uncensored_scan_task
    if not library:
        raise HTTPException(400, "Сначала выберите папку с музыкой")
    if uncensored_scan_task and not uncensored_scan_task.done():
        return _uncensored_scan_snapshot()

    tracks = list(library.values())
    uncensored_scan_state.update(
        status="running",
        processed=0,
        total=len(tracks),
        suspects=0,
        items=[],
        by_reason={},
        error=None,
    )

    async def worker() -> None:
        def progress(processed: int, total: int, suspects: int) -> None:
            uncensored_scan_state.update(
                processed=processed, total=total, suspects=suspects
            )

        try:
            items = await detect_suspects(tracks, lastfm_client, progress)
            counts: dict[str, int] = {}
            for item in items:
                for reason in item.get("reasons", [item["reason"]]):
                    counts[reason] = counts.get(reason, 0) + 1
            uncensored_scan_state.update(
                status="complete",
                processed=len(tracks),
                total=len(tracks),
                suspects=len(items),
                items=items,
                by_reason=counts,
            )
        except Exception as exc:
            _uncensored_log.exception("Censorship scan failed")
            uncensored_scan_state.update(status="error", error=str(exc))

    uncensored_scan_task = asyncio.create_task(worker())
    return _uncensored_scan_snapshot()


@app.get("/api/uncensored/status")
async def uncensored_status() -> dict[str, Any]:
    return _uncensored_scan_snapshot()


@app.get("/api/uncensored/candidates/{track_id}")
async def uncensored_candidates(track_id: str) -> dict[str, Any]:
    track = library.get(track_id)
    if not track:
        raise HTTPException(404, "Трек не в библиотеке")
    item = next(
        (row for row in uncensored_scan_state.get("items", []) if row.get("id") == track_id),
        None,
    )
    if not item:
        raise HTTPException(404, "Сначала просканируйте библиотеку")
    candidates = await find_candidates(track, item.get("canonical_duration"))
    item["candidates"] = candidates
    return {"track_id": track_id, "candidates": candidates}


def _refresh_replaced_track(track_id: str, new_path: str) -> Track:
    replacement = read_track(Path(new_path))
    if replacement is None:
        raise RuntimeError("Не удалось прочитать скачанный трек после замены")
    library.pop(track_id, None)
    library[replacement.id] = replacement
    cache.delete(track_id)
    resolver._lru.pop(track_id, None)
    track_index.delete(track_id)
    track_index.upsert(
        replacement.id,
        path=replacement.path,
        artist=replacement.artist,
        title=replacement.title,
        album=replacement.album,
        duration=replacement.duration,
    )
    return replacement


@app.post("/api/uncensored/replace/{track_id}")
async def uncensored_replace(track_id: str, body: dict[str, Any]) -> dict[str, Any]:
    if body.get("confirmed") is not True:
        raise HTTPException(400, "Требуется явное подтверждение замены")
    track = library.get(track_id)
    if not track:
        raise HTTPException(404, "Трек не в библиотеке")
    item = next(
        (row for row in uncensored_scan_state.get("items", []) if row.get("id") == track_id),
        None,
    )
    if not item:
        raise HTTPException(404, "Трек отсутствует в результатах сканирования")
    try:
        result = await replace_track(
            track,
            str(body.get("candidate_url") or ""),
            item.get("canonical_duration"),
            Path(current_folder or ""),
            _client(),
        )
        updated = _refresh_replaced_track(track_id, result["new_path"])
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        _uncensored_log.exception("Replacement failed for track %s", track_id)
        raise HTTPException(502, f"Замена не выполнена: {exc}") from exc

    _uncensored_log.info(
        "Replaced %s with %s; archived original at %s",
        result["old_path"],
        result["new_path"],
        result["archive_path"],
    )
    return {"ok": True, **result, "track": updated.to_dict()}


@app.get("/api/uncensored/replace_all/status")
async def uncensored_replace_status() -> dict[str, Any]:
    return {**uncensored_replace_state}


@app.post("/api/uncensored/replace_all")
async def uncensored_replace_all(body: dict[str, Any]) -> dict[str, Any]:
    global uncensored_replace_task
    if body.get("confirmed") is not True:
        raise HTTPException(400, "Требуется явное подтверждение массовой замены")
    if uncensored_scan_state["status"] != "complete":
        raise HTTPException(409, "Сначала завершите сканирование библиотеки")
    if uncensored_replace_task and not uncensored_replace_task.done():
        return {"started": False, **uncensored_replace_state}
    items = list(uncensored_scan_state.get("items", []))
    if not items:
        return {"started": False, "total": 0, "message": "Подозрительных треков нет"}
    uncensored_replace_state.update(
        status="running", processed=0, total=len(items), results=[], error=None
    )

    async def worker() -> None:
        results: list[dict[str, Any]] = []
        for index, item in enumerate(items, 1):
            track_id = str(item.get("id") or "")
            track = library.get(track_id)
            row: dict[str, Any] = {"track_id": track_id, "path": item.get("path")}
            try:
                if track is None:
                    raise FileNotFoundError("Трек больше не находится в библиотеке")
                candidates = item.get("candidates") or await find_candidates(
                    track, item.get("canonical_duration")
                )
                if not candidates:
                    raise ValueError("Не найден подходящий официальный кандидат")
                result = await replace_track(
                    track,
                    candidates[0]["url"],
                    item.get("canonical_duration"),
                    Path(current_folder or ""),
                    _client(),
                )
                _refresh_replaced_track(track_id, result["new_path"])
                row.update(status="replaced", **result)
                _uncensored_log.info(
                    "Bulk replaced %s; archived original at %s",
                    result["old_path"],
                    result["archive_path"],
                )
            except Exception as exc:
                row.update(status="skipped", error=str(exc))
                _uncensored_log.exception("Bulk replacement skipped for %s", track_id)
            results.append(row)
            uncensored_replace_state.update(processed=index, results=list(results))
        uncensored_replace_state.update(status="complete", results=results)

    uncensored_replace_task = asyncio.create_task(worker())
    return {"started": True, "total": len(items)}


@app.post("/api/resolve/{track_id}")
async def resolve_one(track_id: str, force: bool = False) -> dict[str, Any]:
    track = library.get(track_id)
    if not track:
        raise HTTPException(404, "Трек не в библиотеке — сначала укажите папку")
    result = await resolver.resolve(_client(), track, force=force)
    await hub.broadcast({"type": "resolved", "track_id": track_id, "media": result})
    return result


@app.post("/api/resolve-all")
async def resolve_all(force: bool = False) -> dict[str, Any]:
    """Фоновое обновление клипов/обложек всей библиотеки."""
    global resolve_task
    if not library:
        raise HTTPException(400, "Библиотека пуста")
    if resolve_task and not resolve_task.done():
        return {"started": False, "message": "Уже идёт обновление"}

    async def worker() -> None:
        items = list(library.values())
        total = len(items)
        await hub.broadcast({"type": "batch_start", "total": total})
        for i, tr in enumerate(items, 1):
            try:
                result = await resolver.resolve(_client(), tr, force=force)
                await hub.broadcast(
                    {
                        "type": "resolved",
                        "track_id": tr.id,
                        "index": i,
                        "total": total,
                        "media": result,
                    }
                )
            except Exception as exc:
                await hub.broadcast(
                    {
                        "type": "error",
                        "track_id": tr.id,
                        "index": i,
                        "total": total,
                        "error": str(exc),
                    }
                )
        await hub.broadcast({"type": "batch_done", "total": total})

    resolve_task = asyncio.create_task(worker())
    return {"started": True, "total": len(library)}


@app.get("/api/stream/{track_id}")
async def stream(track_id: str) -> FileResponse:
    track = library.get(track_id)
    if not track:
        raise HTTPException(404, "Трек не найден")
    path = Path(track.path)
    if not path.is_file():
        raise HTTPException(404, "Файл на диске исчез")
    # Starlette FileResponse поддерживает Range — перемотка в <audio> работает
    mime = "audio/mpeg"
    ext = path.suffix.lower()
    mime_map = {
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".opus": "audio/ogg",
        ".aac": "audio/aac",
        ".wma": "audio/x-ms-wma",
        ".aiff": "audio/aiff",
        ".aif": "audio/aiff",
    }
    return FileResponse(
        path,
        media_type=mime_map.get(ext, mime),
        filename=path.name,
        headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache"},
    )


@app.get("/api/track/{track_id}/waveform")
async def api_waveform(track_id: str) -> dict:
    track = library.get(track_id)
    if not track:
        raise HTTPException(404, "Трек не найден")
    wf, dur = dj_engine.get_waveform(track.id, track.path, track.duration)
    return {"track_id": track.id, "waveform": wf, "duration": dur, "peaks": wf}


@app.get("/api/track/{track_id}/bpm")
async def api_bpm(track_id: str) -> dict:
    track = library.get(track_id)
    if not track:
        raise HTTPException(404, "Трек не найден")
    bpm = dj_engine.get_bpm(track.id, track.path)
    return {"track_id": track.id, "bpm": bpm, "confidence": 0.85}


@app.get("/api/network/status")
async def api_network_status() -> dict[str, Any]:
    # ensure we have at least one check; if unknown, try quick check
    status = network_router.get_status()
    if status.get("lastfm") == "unknown":
        try:
            client = _client()
            status = await network_router.check_once(client)
        except Exception:
            pass
    return status


@app.get("/api/lyrics/{track_id}")
async def lyrics(track_id: str, force: bool = False) -> dict[str, Any]:
    """Текст. Не ждёт клип."""
    track = library.get(track_id)
    if not track:
        raise HTTPException(404, "Трек не в библиотеке")
    data = await fetch_lyrics(_client(), ollama, track, force=force)
    track_index.upsert(
        track.id,
        lyrics=(
            lyrics_cache_path(track).relative_to(ROOT).as_posix()
            if data.get("ok")
            else None
        ),
        lyrics_source=data.get("source"),
    )
    return data


@app.get("/api/cover-find/{track_id}")
async def cover_find(track_id: str) -> dict[str, Any]:
    """Обложка: кэш → Deezer → MB/CAA. Всегда 200. iTunes выключен."""
    clog = logging.getLogger("kadr.covers")
    track = library.get(track_id)
    if not track:
        clog.info("cover-find %s artist=? track=? deezer=- itunes=- mb=- result=no-track", track_id)
        return {"ok": True, "track_id": track_id, "url": None, "cover_file": None, "placeholder": True}
    cover, report = await fetch_cover_report(
        _client(),
        track.id,
        track.artist,
        track.title,
        track.album,
        embedded=track.embedded_cover,
    )
    if not cover:
        ph = generate_placeholder(track.id, track.artist, track.title)
        cover = {"source": "placeholder", "file": ph.name, "path": str(ph)}
    placeholder = cover.get("source") == "placeholder"
    result = "ph" if placeholder else cover.get("source")
    clog.info(
        "cover-find %s artist=%s track=%s deezer=%s itunes=%s mb=%s result=%s",
        track.id,
        track.artist,
        track.title,
        report.get("deezer"),
        report.get("itunes"),
        report.get("mb"),
        result,
    )
    track_index.upsert(
        track.id,
        cover=f"cache/covers/{cover['file']}",
        cover_source=cover.get("source"),
    )
    return {
        "ok": True,
        "track_id": track.id,
        "url": None if placeholder else f"/api/cover/{cover['file']}",
        "cover_file": cover["file"],
        "cover_source": cover.get("source"),
        "placeholder": placeholder,
    }


@app.get("/api/cover-proxy")
async def cover_proxy(url: str = Query(..., min_length=8)) -> Response:
    """Прокси картинки. Всегда 200 (placeholder, если скачать не вышло)."""
    got = await proxy_cover(_client(), url)
    if got:
        body, mime = got
        return Response(
            content=body,
            media_type=mime,
            headers={"Cache-Control": "public, max-age=2592000"},
        )
    ph = generate_placeholder("proxy", "Курымдык", "no image")
    return Response(
        content=ph.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=600"},
    )


@app.get("/api/cover/{filename}")
async def cover(filename: str) -> Response:
    name = Path(filename).name
    for folder in (COVERS_DIR, EMBEDDED_DIR):
        path = folder / name
        if path.is_file():
            return FileResponse(path, headers={"Cache-Control": "public, max-age=2592000"})
    ph = generate_placeholder("missing", "Курымдык", "no cover")
    return Response(
        content=ph.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=600"},
    )


# ---------- Файловый браузер (локальный ПК) ----------

def _list_windows_drives() -> list[str]:
    drives = []
    if os.name == "nt":
        import string

        for letter in string.ascii_uppercase:
            d = f"{letter}:\\"
            if os.path.isdir(d):
                drives.append(d)
    return drives


@app.get("/api/fs/roots")
async def fs_roots() -> dict[str, Any]:
    roots: list[dict[str, str]] = []
    home = str(Path.home())
    roots.append({"name": "Домашняя папка", "path": home})
    music = Path.home() / "Music"
    if music.is_dir():
        roots.append({"name": "Музыка", "path": str(music)})
    # Типичные папки Windows
    for extra in ("Музыка", "Music", "Downloads", "Загрузки"):
        p = Path.home() / extra
        if p.is_dir() and str(p) not in {r["path"] for r in roots}:
            roots.append({"name": extra, "path": str(p)})
    for d in _list_windows_drives():
        roots.append({"name": d, "path": d})
    if os.name != "nt":
        roots.append({"name": "/", "path": "/"})
    return {"roots": roots}


@app.get("/api/fs/list")
async def fs_list(path: str = Query(..., min_length=1)) -> dict[str, Any]:
    p = Path(path).expanduser()
    if not p.exists():
        raise HTTPException(404, "Путь не существует")
    if not p.is_dir():
        p = p.parent
    try:
        p = p.resolve()
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc

    dirs: list[dict[str, Any]] = []
    music_here = 0
    try:
        entries = list(p.iterdir())
    except PermissionError:
        raise HTTPException(403, "Нет доступа к папке")

    for child in sorted(entries, key=lambda x: x.name.lower()):
        if child.name.startswith("."):
            continue
        try:
            if child.is_dir():
                dirs.append({"name": child.name, "path": str(child)})
            elif child.is_file() and child.suffix.lower() in {
                ".mp3", ".flac", ".wav", ".m4a", ".ogg", ".opus", ".aac",
            }:
                music_here += 1
        except OSError:
            continue

    parent = str(p.parent) if p.parent != p else None
    return {
        "path": str(p),
        "parent": parent,
        "dirs": dirs,
        "music_count": music_here,
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await hub.connect(ws)
    try:
        await ws.send_json({"type": "hello", "ollama": ollama.status.to_dict()})
        while True:
            # Держим соединение, клиент может пинговать
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        hub.disconnect(ws)
    except Exception:
        hub.disconnect(ws)


@app.get("/api/cache")
async def get_cache() -> JSONResponse:
    return JSONResponse(cache.all_tracks())


@app.post("/api/cache/clear")
async def clear_cache() -> dict[str, bool]:
    cache.clear()
    return {"ok": True}


# ---------- Статика ----------
# CSS/JS
if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/manifest.json")
async def manifest() -> FileResponse:
    path = FRONTEND_DIR / "manifest.json"
    if not path.is_file():
        raise HTTPException(404, "manifest.json")
    return FileResponse(path, media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    path = FRONTEND_DIR / "sw.js"
    if not path.is_file():
        raise HTTPException(404, "sw.js")
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/")
async def index_page() -> FileResponse:
    page = FRONTEND_DIR / "index.html"
    if not page.is_file():
        raise HTTPException(500, "Не найден frontend/index.html")
    return FileResponse(page)


def run() -> None:
    import uvicorn

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "asyncio"):
        logging.getLogger(name).addFilter(_quiet)
    uvicorn.run(
        "backend.main:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()

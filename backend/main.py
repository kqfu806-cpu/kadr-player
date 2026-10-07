"""
Курымдык — локальный медиаплеер.
Backend: FastAPI. Все ИИ-запросы только на http://127.0.0.1:11434 (Ollama).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.requests import ClientDisconnect
from fastapi.staticfiles import StaticFiles

from .artists import build_artists
from .cache_store import CacheStore
from .config import (
    CACHE_DIR,
    COVERS_DIR,
    EMBEDDED_DIR,
    FRONTEND_DIR,
    HOST,
    OLLAMA_URL,
    PORT,
    ROOT,
    USER_AGENT,
)
from .censorship_detector import detect_suspects
from .covers import fetch_cover_report, proxy_cover
from .index_store import IndexStore
from .lyrics import fetch_lyrics, lyrics_cache_path
from .media_resolver import MediaResolver
from .ollama_ai import OllamaClient
from .placeholder import generate_placeholder
from .releases import fetch_weekly_releases
from .scanner import Track, read_track, scan_folder
from .uncensored_finder import find_candidates
from .uncensored_replacer import replace_track

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

cache = CacheStore()
track_index = IndexStore()
ollama = OllamaClient(OLLAMA_URL)
resolver = MediaResolver(cache, ollama, track_index)

# Состояние библиотеки в памяти
library: dict[str, Track] = {}
current_folder: str | None = None
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
    await ollama.refresh_status(http_client)
    st = ollama.status.to_dict()
    print("=== Курымдык ===")
    print(f"UI:     http://{HOST}:{PORT}")
    print(f"Ollama: {st['url']}  online={st['online']}  models={st['models']}")


@app.on_event("shutdown")
async def _shutdown() -> None:
    if http_client:
        await http_client.aclose()


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
        "folder": current_folder,
        "tracks": len(library),
        "ollama": ollama.status.to_dict(),
    }


@app.get("/api/artists")
async def api_artists() -> dict[str, Any]:
    """Уникальные исполнители + похожие (nomic-embed-text, если Ollama жива)."""
    tracks = list(library.values())
    return await build_artists(_client(), ollama, tracks)


@app.post("/api/new-releases")
async def api_new_releases(body: dict[str, Any]) -> dict[str, Any]:
    artists = body.get("artists") if isinstance(body, dict) else []
    if not isinstance(artists, list):
        artists = []
    local_tracks = [
        {"artist": track.artist, "title": track.title, "album": track.album}
        for track in library.values()
    ]
    try:
        days = int(body.get("days", 7))
    except (TypeError, ValueError):
        days = 7
    return await fetch_weekly_releases(_client(), artists, days, local_tracks)


@app.get("/api/library")
async def get_library() -> dict[str, Any]:
    tracks = [_track_public(t) for t in library.values()]
    # Сохраняем порядок сканирования (альбом / номер)
    return {"folder": current_folder, "count": len(tracks), "tracks": tracks}


@app.post("/api/scan")
async def scan(body: dict[str, Any]) -> dict[str, Any]:
    """Сканируем папку с музыкой. Резолв клипов — ленивый, по запросу."""
    global library, current_folder
    folder = (body or {}).get("path") or ""
    folder = str(folder).strip().strip('"')
    if not folder:
        raise HTTPException(400, "Укажите путь к папке")
    p = Path(folder)
    if not p.exists() or not p.is_dir():
        raise HTTPException(400, f"Папка не найдена: {folder}")

    tracks = scan_folder(str(p.resolve()))
    library = {t.id: t for t in tracks}
    current_folder = str(p.resolve())
    return {
        "folder": current_folder,
        "count": len(tracks),
        "tracks": [_track_public(t) for t in tracks],
    }


def _uncensored_scan_snapshot() -> dict[str, Any]:
    return {**uncensored_scan_state}


@app.get("/api/uncensored/scan")
async def uncensored_scan() -> dict[str, Any]:
    """Start a rate-limited MusicBrainz scan, or return its current result."""
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
            items = await detect_suspects(tracks, _client(), progress)
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

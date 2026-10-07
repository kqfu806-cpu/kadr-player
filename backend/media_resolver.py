"""Оркестратор: клип на YouTube или обложка. Результат пишем в кэш."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any, Callable, Awaitable

import httpx

from .cache_store import CacheStore
from .config import YOUTUBE_TOP_FOR_LLM
from .index_store import IndexStore
from .ollama_ai import OllamaClient, _BAD_RE, _cosine, _tokens
from .scanner import Track
from .youtube_search import search_youtube, youtube_thumb

# Старые записи кэша без этой версии пересчитываем (ошибочные клипы)
CLIP_MATCH_VER = 3


ProgressCb = Callable[[dict[str, Any]], Awaitable[None]]


class MediaResolver:
    def __init__(self, cache: CacheStore, ollama: OllamaClient, index: IndexStore | None = None) -> None:
        self.cache = cache
        self.ollama = ollama
        self.index = index
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Semaphore(2)
        self._lru: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self._lru_max = 200

    def _lock_for(self, track_id: str) -> asyncio.Lock:
        if track_id not in self._locks:
            self._locks[track_id] = asyncio.Lock()
        return self._locks[track_id]

    def cached(self, track_id: str) -> dict[str, Any] | None:
        if track_id in self._lru:
            self._lru.move_to_end(track_id)
            return self._lru[track_id]
        return self.cache.get(track_id)

    def _remember(self, track_id: str, result: dict[str, Any]) -> dict[str, Any]:
        self._lru[track_id] = result
        self._lru.move_to_end(track_id)
        while len(self._lru) > self._lru_max:
            self._lru.popitem(last=False)
        return result

    async def resolve(
        self,
        client: httpx.AsyncClient,
        track: Track,
        force: bool = False,
        progress: ProgressCb | None = None,
    ) -> dict[str, Any]:
        """Клип. Повторные вызовы того же трека ждут один Future / LRU."""
        if not force:
            hit = self.cached(track.id)
            if hit and hit.get("status") == "ok":
                if hit.get("display") == "clip" and hit.get("match_ver") != CLIP_MATCH_VER:
                    hit = None
                if hit:
                    return hit
        existing = self._inflight.get(track.id)
        if existing and not force:
            return await existing

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._inflight[track.id] = fut
        try:
            async with self._lock_for(track.id):
                if not force:
                    hit = self.cache.get(track.id)
                    if hit and hit.get("status") == "ok":
                        if not (
                            hit.get("display") == "clip"
                            and hit.get("match_ver") != CLIP_MATCH_VER
                        ):
                            self._remember(track.id, hit)
                            fut.set_result(hit)
                            return hit
                async with self._global:
                    result = await self._resolve_inner(client, track, progress)
                self.cache.put(track.id, result)
                if self.index:
                    clip = None
                    if result.get("youtube_id"):
                        clip = {
                            "url": result["youtube_id"],
                            "source": "youtube",
                            "checked_at": result.get("updated_at"),
                        }
                    self.index.upsert(
                        track.id,
                        clip=clip,
                        clip_checked_sources=["youtube"],
                    )
                self._remember(track.id, result)
                if not fut.done():
                    fut.set_result(result)
                return result
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            if self._inflight.get(track.id) is fut:
                self._inflight.pop(track.id, None)

    async def _emit(self, progress: ProgressCb | None, payload: dict[str, Any]) -> None:
        if progress:
            try:
                await progress(payload)
            except Exception:
                pass

    async def _resolve_inner(
        self,
        client: httpx.AsyncClient,
        track: Track,
        progress: ProgressCb | None,
    ) -> dict[str, Any]:
        await self.ollama.refresh_status(client)

        artist, title, album = track.artist, track.title, track.album

        # Если тегов нет — просим LLM разобрать имя файла
        if not track.tags_ok and self.ollama.status.models.get("deepseek-r1:8b"):
            parsed = await self.ollama.parse_filename(client, track.filename)
            if parsed:
                artist = parsed.get("artist") or artist
                title = parsed.get("title") or title

        await self._emit(progress, {"stage": "youtube", "track_id": track.id})

        clip = None
        reject_why: str | None = None
        candidates_info: list[dict[str, Any]] = []
        try:
            cands = await search_youtube(client, artist, title, track.duration)
        except Exception:
            cands = []

        if cands:
            ranked = await self.ollama.rank_by_embeddings(client, artist, title, cands)
            top = ranked[:YOUTUBE_TOP_FOR_LLM]
            candidates_info = [
                {**c.to_dict(), "score": round(s, 4)} for c, s in top
            ]
            await self._emit(progress, {"stage": "llm", "track_id": track.id})
            clip = await self.ollama.pick_official_clip(
                client, artist, title, album, track.duration, top
            )
            if clip and clip.get("video_id"):
                ok, why = await self._guard_clip(client, track, clip, top)
                if not ok:
                    clip = None
                    reject_why = why

        if clip and clip.get("video_id"):
            return {
                "status": "ok",
                "track_id": track.id,
                "display": "clip",
                "artist": artist,
                "title": title,
                "album": album,
                "youtube_id": clip["video_id"],
                "youtube_title": clip.get("youtube_title"),
                "channel": clip.get("channel"),
                "confidence": clip.get("confidence"),
                "reason": clip.get("reason"),
                "picker": clip.get("picker"),
                "thumb": youtube_thumb(clip["video_id"]),
                "cover_file": None,
                "cover_source": None,
                "candidates": candidates_info,
                "match_ver": CLIP_MATCH_VER,
            }

        reason = (
            f"Клип отклонён ({reject_why})"
            if reject_why
            else "Официальный клип не найден"
        )
        return {
            "status": "ok",
            "track_id": track.id,
            "display": "none",
            "artist": artist,
            "title": title,
            "album": album,
            "youtube_id": None,
            "cover_file": None,
            "cover_source": None,
            "confidence": 0.0,
            "reason": reason,
            "picker": None,
            "thumb": None,
            "candidates": candidates_info,
            "match_ver": CLIP_MATCH_VER,
        }

    async def _guard_clip(
        self,
        client: httpx.AsyncClient,
        track: Track,
        clip: dict[str, Any],
        top: list,
    ) -> tuple[bool, str]:
        """Двойная проверка: чёрный список, длительность ±20%, эмбеддинги ≥0.85."""
        yt_title = clip.get("youtube_title") or ""
        track_blob = f"{track.artist} {track.title}".lower()
        m = _BAD_RE.search(yt_title)
        if m:
            bad = m.group(0).lower()
            if bad not in track_blob:
                return False, f"blacklist:{bad}"

        td = float(track.duration or 0)
        cd = float(clip.get("duration_sec") or 0)
        if td > 30 and cd > 0:
            diff = abs(cd - td)
            if cd >= td:
                if diff > max(td * 0.20, 45):
                    return False, "duration"
            elif diff / td > 0.20:
                return False, "duration"

        q = f"{track.artist} {track.title}"
        qvec = await self.ollama.embed(client, q)
        vvec = await self.ollama.embed(client, yt_title)
        if qvec and vvec:
            sim = _cosine(qvec, vvec)
            clip["embed_sim"] = round(sim, 4)
            if sim >= 0.85:
                return True, "embed"
            tt = _tokens(track.title)
            vt = _tokens(yt_title)
            overlap = (len(tt & vt) / len(tt)) if tt else 0.0
            if sim >= 0.72 and overlap >= 0.45:
                return True, "embed-lex"
            return False, f"embed:{sim:.2f}"

        tt = _tokens(track.title)
        vt = _tokens(yt_title)
        if tt and len(tt & vt) / len(tt) < 0.45:
            return False, "lexical"
        return True, "lex"

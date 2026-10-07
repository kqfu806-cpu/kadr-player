"""Клиент локальной Ollama (deepseek-r1:8b, llava:7b, nomic-embed-text)."""

from __future__ import annotations

import base64
import json
import re
import time
from io import BytesIO
from typing import Any

import httpx
from PIL import Image

from .config import (
    OLLAMA_EMBED,
    OLLAMA_EMBED_NUM_CTX,
    OLLAMA_EMBED_TIMEOUT,
    OLLAMA_LLM,
    OLLAMA_LLM_NUM_CTX,
    OLLAMA_LLM_TIMEOUT,
    OLLAMA_NUM_GPU,
    OLLAMA_TAGS_TIMEOUT,
    OLLAMA_URL,
    OLLAMA_VISION,
    OLLAMA_VISION_NUM_CTX,
    OLLAMA_VISION_TIMEOUT,
)
from .youtube_search import YTCandidate


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    return dot / ((na ** 0.5) * (nb ** 0.5) + 1e-9)


def extract_json(text: str) -> dict[str, Any] | None:
    """Вытаскиваем JSON из ответа LLM (включая deepseek-r1 с <think>)."""
    if not text:
        return None
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    cleaned = cleaned.strip()
    # Сначала блок ```json
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1)
    # Ищем первый JSON-объект
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    blob = cleaned[start : end + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        blob2 = re.sub(r",\s*}", "}", blob)
        blob2 = re.sub(r",\s*]", "]", blob2)
        try:
            return json.loads(blob2)
        except json.JSONDecodeError:
            return None


class OllamaStatus:
    def __init__(self) -> None:
        self.online = False
        self.models: dict[str, bool] = {
            OLLAMA_LLM: False,
            OLLAMA_VISION: False,
            OLLAMA_EMBED: False,
        }
        self.error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "url": OLLAMA_URL,
            "models": dict(self.models),
            "error": self.error,
            "mode": "ai" if self.online else "heuristic",
        }


class OllamaClient:
    def __init__(self, base: str = OLLAMA_URL) -> None:
        self.base = base.rstrip("/")
        self.status = OllamaStatus()
        self._last_refresh = 0.0

    async def _maybe_revive(self, client: httpx.AsyncClient) -> None:
        """Если Ollama была offline — перепроверить не чаще раза в 30 с."""
        if self.status.online:
            return
        now = time.monotonic()
        if now - self._last_refresh < 30:
            return
        await self.refresh_status(client)

    async def refresh_status(self, client: httpx.AsyncClient) -> OllamaStatus:
        self._last_refresh = time.monotonic()
        try:
            r = await client.get(f"{self.base}/api/tags", timeout=OLLAMA_TAGS_TIMEOUT)
            r.raise_for_status()
            names = {m.get("name", "") for m in (r.json().get("models") or [])}
            # ollama иногда отдаёт "deepseek-r1:8b" или "deepseek-r1:8b-..."
            def has(model: str) -> bool:
                if model in names:
                    return True
                base = model.split(":")[0]
                return any(n == model or n.startswith(base + ":") or n.startswith(model) for n in names)

            self.status.online = True
            self.status.error = None
            self.status.models[OLLAMA_LLM] = has(OLLAMA_LLM)
            self.status.models[OLLAMA_VISION] = has(OLLAMA_VISION)
            self.status.models[OLLAMA_EMBED] = has(OLLAMA_EMBED)
        except Exception as exc:
            self.status.online = False
            self.status.error = f"Ollama недоступна: {exc.__class__.__name__}"
            for k in self.status.models:
                self.status.models[k] = False
        return self.status

    async def embed(self, client: httpx.AsyncClient, text: str) -> list[float] | None:
        await self._maybe_revive(client)
        if not self.status.models.get(OLLAMA_EMBED):
            return None
        payload_variants = [
            (
                "/api/embeddings",
                {
                    "model": OLLAMA_EMBED,
                    "prompt": text,
                    "options": {"num_ctx": OLLAMA_EMBED_NUM_CTX},
                },
            ),
            (
                "/api/embed",
                {
                    "model": OLLAMA_EMBED,
                    "input": text,
                    "options": {"num_ctx": OLLAMA_EMBED_NUM_CTX},
                },
            ),
        ]
        for path, body in payload_variants:
            try:
                r = await client.post(
                    f"{self.base}{path}",
                    json=body,
                    timeout=OLLAMA_EMBED_TIMEOUT,
                )
                if r.status_code >= 400:
                    continue
                data = r.json()
                if "embedding" in data:
                    return list(data["embedding"])
                if "embeddings" in data and data["embeddings"]:
                    return list(data["embeddings"][0])
            except Exception:
                continue
        return None

    async def generate(
        self,
        client: httpx.AsyncClient,
        prompt: str,
        timeout: float | None = None,
        num_predict: int = 400,
    ) -> str | None:
        await self._maybe_revive(client)
        if not self.status.models.get(OLLAMA_LLM):
            return None
        try:
            r = await client.post(
                f"{self.base}/api/generate",
                json={
                    "model": OLLAMA_LLM,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.1,
                        "num_predict": num_predict,
                        "num_ctx": OLLAMA_LLM_NUM_CTX,
                        "num_gpu": OLLAMA_NUM_GPU,
                        "num_thread": 8,
                    },
                },
                timeout=timeout or OLLAMA_LLM_TIMEOUT,
            )
            r.raise_for_status()
            return r.json().get("response") or ""
        except Exception:
            return None

    async def vision(
        self, client: httpx.AsyncClient, prompt: str, image_path: str
    ) -> str | None:
        await self._maybe_revive(client)
        if not self.status.models.get(OLLAMA_VISION):
            return None
        try:
            with Image.open(image_path) as im:
                im = im.convert("RGB")
                im.thumbnail((512, 512))
                buf = BytesIO()
                im.save(buf, format="JPEG", quality=85)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            return None
        try:
            r = await client.post(
                f"{self.base}/api/generate",
                json={
                    "model": OLLAMA_VISION,
                    "prompt": prompt,
                    "images": [b64],
                    "stream": False,
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 250,
                        "num_ctx": OLLAMA_VISION_NUM_CTX,
                        "num_gpu": OLLAMA_NUM_GPU,
                    },
                },
                timeout=OLLAMA_VISION_TIMEOUT,
            )
            r.raise_for_status()
            return r.json().get("response") or ""
        except Exception:
            return None

    async def rank_by_embeddings(
        self,
        client: httpx.AsyncClient,
        artist: str,
        title: str,
        candidates: list[YTCandidate],
    ) -> list[tuple[YTCandidate, float]]:
        """Семантическое ранжирование названий роликов относительно трека."""
        query = f"official music video clip {artist} — {title}"
        qvec = await self.embed(client, query)
        scored: list[tuple[YTCandidate, float]] = []
        if qvec is None:
            # Фолбэк — лексическое сходство
            for c in candidates:
                scored.append((c, lexical_score(artist, title, c)))
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored

        for c in candidates:
            text = f"{c.title} by {c.channel}"
            vec = await self.embed(client, text)
            sim = _cosine(qvec, vec) if vec else 0.0
            # Небольшой бонус за «official», чтобы эмбеддинги не промахнулись
            bonus = heuristic_bonus(c, artist)
            scored.append((c, sim + bonus * 0.05))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    async def pick_official_clip(
        self,
        client: httpx.AsyncClient,
        artist: str,
        title: str,
        album: str,
        duration: float,
        top: list[tuple[YTCandidate, float]],
    ) -> dict[str, Any] | None:
        """
        Арбитр deepseek-r1:8b выбирает ОДИН официальный клип или null.
        Если модели нет — эвристика.
        """
        if not top:
            return None

        if len(top) == 1:
            candidate, score = top[0]
            if score >= 0.68 and _fuzzy_has(candidate.title, title) and _channel_trusted(candidate.channel, artist):
                return {
                    "video_id": candidate.video_id,
                    "youtube_title": candidate.title,
                    "channel": candidate.channel,
                    "duration_sec": candidate.duration_sec,
                    "confidence": 0.9,
                    "reason": "Точное название на канале исполнителя.",
                    "picker": "verified-local-match",
                }

        lines = []
        for i, (c, score) in enumerate(top):
            lines.append(
                f"{i}. video_id={c.video_id} | title={c.title!r} | channel={c.channel!r} "
                f"| duration={c.duration_sec}s | views={c.views} | embed_score={score:.3f}"
            )
        prompt = f"""Ты арбитр музыкального плеера. Нужно выбрать ОФИЦИАЛЬНЫЙ ВИДЕОКЛИП трека.

Трек:
- artist: {artist}
- title: {title}
- album: {album}
- duration_sec: {int(duration) if duration else 0}

Кандидаты YouTube (индекс с нуля):
{chr(10).join(lines)}

Правила:
- Официальный клип: "official video", "official music video", "официальный клип", канал артиста/лейбла.
- НЕ клип: lyrics, audio, visualizer, cover, live, concert, karaoke, remix (если трек не remix), nightcore, speed up, 8D, fan-made.
- Название и артист должны совпадать по смыслу (язык может отличаться: "Группа крови" = "Gruppa krovi" = "Blood Type").
- Длительность клипа обычно близка к длительности трека (±90 секунд), но intro клипа допустим.
- Если ни один кандидат не является официальным клипом — video_id = null.
- Не выдумывай video_id.

Верни ТОЛЬКО JSON без markdown:
{{"video_id": "ID или null", "confidence": 0.0, "reason": "кратко на русском"}}
"""
        raw = await self.generate(client, prompt)
        parsed = extract_json(raw or "")
        if parsed:
            vid = parsed.get("video_id")
            if vid in ("null", "None", "", None):
                return None
            vid = str(vid).strip()
            allowed = {c.video_id for c, _ in top}
            if vid not in allowed:
                # Модель могла вернуть индекс
                try:
                    idx = int(vid)
                    if 0 <= idx < len(top):
                        vid = top[idx][0].video_id
                    elif 1 <= idx <= len(top):
                        vid = top[idx - 1][0].video_id
                    else:
                        return None
                except ValueError:
                    return None
            cand = next(c for c, _ in top if c.video_id == vid)
            conf = float(parsed.get("confidence") or 0.6)
            if conf < 0.42:
                return None
            return {
                "video_id": cand.video_id,
                "youtube_title": cand.title,
                "channel": cand.channel,
                "duration_sec": cand.duration_sec,
                "confidence": conf,
                "reason": str(parsed.get("reason") or ""),
                "picker": OLLAMA_LLM,
            }

        # Эвристический фолбэк
        return heuristic_pick(artist, title, duration, top)

    async def parse_filename(
        self, client: httpx.AsyncClient, filename: str
    ) -> dict[str, str] | None:
        """Парсим грязное имя файла через LLM, если тегов нет."""
        prompt = f"""Из имени аудиофайла извлеки исполнителя и название трека.
Имя файла: {filename}

Верни ТОЛЬКО JSON:
{{"artist": "...", "title": "..."}}
Если понять нельзя — пустые строки.
"""
        raw = await self.generate(client, prompt)
        parsed = extract_json(raw or "")
        if not parsed:
            return None
        artist = str(parsed.get("artist") or "").strip()
        title = str(parsed.get("title") or "").strip()
        if artist or title:
            return {"artist": artist, "title": title}
        return None

    async def verify_cover(
        self,
        client: httpx.AsyncClient,
        image_path: str,
        artist: str,
        title: str,
        album: str,
    ) -> bool:
        """llava:7b — выглядит ли картинка как обложка этого релиза? При сомнении — True."""
        prompt = (
            f"Это обложка музыкального релиза? Ожидается артист «{artist}», "
            f"трек «{title}», альбом «{album}». "
            "Если это явно другой артист, мем, скриншот или реклама — не подходит. "
            'Ответь ТОЛЬКО JSON: {"ok": true} или {"ok": false, "reason": "..."}'
        )
        raw = await self.vision(client, prompt, image_path)
        parsed = extract_json(raw or "")
        if not parsed:
            return True  # не блокируем из-за сбоя зрения
        return bool(parsed.get("ok", True))


# --- Эвристики, если Ollama выключена ---

_OFFICIAL_RE = re.compile(
    r"official\s*(music\s*)?video|официал(ьный)?\s*клип|официал(ьное)?\s*видео|"
    r"\bclip officiel\b|\bm\/?v\b|\bмуз(?:ыкальный)?\s*клип\b",
    re.I,
)
_BAD_RE = re.compile(
    r"\b(lyrics?|lyric video|audio(?:\s+only)?|visualizer|cover\b|karaoke|live\b|concert|"
    r"nightcore|8d(?:\s+audio)?|slowed|reverb|speed[\s-]?up|sped\s+up|fan\s*made|full album|"
    r"hour|remix|mashup|reaction|tutorial|how to play|на пианино|разбор|"
    r"на русском|на украинском|перевод|minus|instrumental|piano)\b",
    re.I,
)


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[0-9a-zа-яё]+", (s or "").lower()))


def lexical_score(artist: str, title: str, c: YTCandidate) -> float:
    q = _tokens(artist) | _tokens(title)
    t = _tokens(c.title) | _tokens(c.channel)
    if not q:
        return 0.0
    overlap = len(q & t) / len(q)
    return overlap + heuristic_bonus(c, artist) * 0.08


_CHANNEL_TRUST_RE = re.compile(
    r"official|vevo|topic|records|label|официал|лейбл",
    re.I,
)


def _fuzzy_has(hay: str, needle: str) -> bool:
    if not hay or not needle:
        return False
    h = hay.lower()
    n = needle.lower()
    if n in h:
        return True
    ht, nt = _tokens(h), _tokens(n)
    if not nt:
        return False
    return len(ht & nt) / len(nt) >= 0.6


def _channel_trusted(channel: str, artist: str) -> bool:
    ch = channel or ""
    if _CHANNEL_TRUST_RE.search(ch):
        return True
    return bool(artist) and _fuzzy_has(ch, artist)


def heuristic_bonus(c: YTCandidate, artist: str = "") -> float:
    title = c.title or ""
    bonus = 0.0
    if _OFFICIAL_RE.search(title):
        bonus += 4.0
    if _BAD_RE.search(title):
        bonus -= 4.5
    if "vevo" in (c.channel or "").lower():
        bonus += 2.0
    if "official" in (c.channel or "").lower() or "официал" in (c.channel or "").lower():
        bonus += 1.5
    if artist and not _channel_trusted(c.channel or "", artist):
        bonus -= 3.0
    if artist:
        tl = title.lstrip().lower()
        al = artist.lower()
        if tl.startswith(al):
            bonus += 1.0
    return bonus


def heuristic_pick(
    artist: str,
    title: str,
    duration: float,
    top: list[tuple[YTCandidate, float]],
) -> dict[str, Any] | None:
    ranked: list[tuple[float, YTCandidate]] = []
    at = _tokens(artist) | _tokens(title)
    for c, emb in top:
        s = emb * 5 + heuristic_bonus(c, artist)
        ct = _tokens(c.title)
        if at and len(at & ct) / len(at) < 0.3:
            s -= 2.0
        else:
            s += 1.5
        if duration and c.duration_sec:
            diff = abs(c.duration_sec - duration)
            if diff <= 25:
                s += 2.0
            elif diff <= 90:
                s += 0.8
            elif duration > 60 and diff > 25:
                s -= 1.5
            elif diff > 180:
                s -= 2.0
        ranked.append((s, c))
    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked:
        return None
    best_s, best = ranked[0]
    need = 4.5
    if not _OFFICIAL_RE.search(best.title or ""):
        need = 4.5
    if not _channel_trusted(best.channel or "", artist):
        need = max(need, 5.5)
    if best_s < need:
        return None
    conf = max(0.45, min(0.93, best_s / 10.0))
    if duration and duration > 60 and best.duration_sec:
        if abs(float(best.duration_sec) - float(duration)) > 25:
            conf -= 0.25
    if conf < 0.55:
        return None
    return {
        "video_id": best.video_id,
        "youtube_title": best.title,
        "channel": best.channel,
        "duration_sec": best.duration_sec,
        "confidence": conf,
        "reason": "Эвристический выбор (Ollama недоступна или не вернула JSON)",
        "picker": "heuristic",
    }

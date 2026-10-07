"""Cached, local-only translation of English artist biographies."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

import httpx

from .config import CACHE_DIR
from .ollama_ai import OllamaClient

TRANSLATIONS_DIR = CACHE_DIR / "translations"
log = logging.getLogger("kadr.translator")


def _is_english(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    if len(letters) < 40:
        return False
    latin = sum("a" <= char.casefold() <= "z" for char in letters)
    cyrillic = sum("\u0400" <= char <= "\u04ff" for char in letters)
    return latin / len(letters) >= 0.65 and latin > cyrillic


def _cache_path(text: str, cache_dir: Path) -> Path:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.json"


async def translate_biography(
    text: str,
    client: httpx.AsyncClient,
    ollama: OllamaClient,
    cache_dir: Path | None = None,
) -> str:
    """Translate an English biography to Russian; retain source text on failure."""
    original = re.sub(r"\s+", " ", text).strip()
    if not original or not _is_english(original):
        return original

    directory = cache_dir or TRANSLATIONS_DIR
    path = _cache_path(original, directory)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("source_sha256") == path.stem and isinstance(cached.get("translation"), str):
            return cached["translation"]
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        log.warning("Could not read translation cache %s: %s", path, type(exc).__name__)

    prompt = (
        "Переведи биографию музыкального исполнителя с английского на русский. "
        "Сохрани имена, факты и ссылки. Верни только перевод, без пояснений.\n\n"
        f"{original}"
    )
    translated = await ollama.generate(client, prompt, timeout=30, num_predict=900)
    translated = re.sub(r"^\s*<think>.*?</think>\s*", "", translated or "", flags=re.S | re.I).strip()
    if not translated or not any("\u0400" <= char <= "\u04ff" for char in translated):
        log.warning("Local model did not return a Russian translation")
        return original

    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"source_sha256": path.stem, "translation": translated},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        log.warning("Could not cache translated biography: %s", type(exc).__name__)
    return translated

"""Async Last.fm API client with a small on-disk response cache."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from .config import ROOT, USER_AGENT
from .lastfm_config import get_lastfm_api_key

API_URL = "https://ws.audioscrobbler.com/2.0/"
CACHE_TTL_SECONDS = 24 * 60 * 60
MIN_REQUEST_INTERVAL = 0.2


class LastFmError(RuntimeError):
    """Base error for Last.fm configuration, transport, and API failures."""


class LastFmConfigurationError(LastFmError):
    """Raised when a cold-cache API request has no configured key."""


class LastFmApiError(LastFmError):
    """Raised when Last.fm rejects a request or returns an invalid response."""


def _logger() -> logging.Logger:
    logger = logging.getLogger("kadr.lastfm")
    logger.setLevel(logging.INFO)
    logger.propagate = True
    tools_dir = ROOT / ".tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    log_path = tools_dir / "lastfm.log"
    if not any(
        isinstance(handler, logging.FileHandler)
        and Path(handler.baseFilename) == log_path.resolve()
        for handler in logger.handlers
    ):
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _response_items(
    payload: dict[str, Any], section: str, field: str
) -> list[dict[str, Any]]:
    group = payload.get(section)
    if group is None:
        return []
    if not isinstance(group, dict):
        raise LastFmApiError(f"Last.fm returned an invalid {section} response")
    value = group.get(field)
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        if all(isinstance(item, dict) for item in value):
            return value
    raise LastFmApiError(f"Last.fm returned an invalid {section}.{field} response")


class LastFmClient:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        cache_dir: Path | None = None,
        api_key: str | None = None,
    ) -> None:
        self.client = client
        self.cache_dir = cache_dir or ROOT / ".tools" / "lastfm_cache"
        self.api_key = api_key
        self.log = _logger()
        self._rate_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def get_similar_artists(
        self, artist: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "artist.getSimilar", {"artist": self._required(artist, "artist"), "limit": self._limit(limit)}
        )
        return _response_items(payload, "similarartists", "artist")

    async def get_artist_top_tracks(
        self, artist: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "artist.getTopTracks", {"artist": self._required(artist, "artist"), "limit": self._limit(limit)}
        )
        return _response_items(payload, "toptracks", "track")

    async def get_artist_info(self, artist: str) -> dict[str, Any]:
        payload = await self._request(
            "artist.getInfo", {"artist": self._required(artist, "artist")}
        )
        info = payload.get("artist")
        if not isinstance(info, dict):
            raise LastFmApiError("Last.fm returned no artist information")
        return info

    async def get_similar_tracks(
        self, artist: str, title: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "track.getSimilar",
            {
                "artist": self._required(artist, "artist"),
                "track": self._required(title, "title"),
                "limit": self._limit(limit),
            },
        )
        return _response_items(payload, "similartracks", "track")

    async def _request(self, method: str, params: dict[str, str | int]) -> dict[str, Any]:
        cache_path = self._cache_path(method, params)
        cached = self._read_cache(cache_path)
        if cached is not None:
            return cached

        api_key = self.api_key or get_lastfm_api_key()
        if not api_key:
            raise LastFmConfigurationError("LASTFM_API_KEY is required for an uncached request")

        request_params: dict[str, str | int] = {
            **params,
            "method": method,
            "api_key": api_key,
            "format": "json",
        }
        for attempt in range(3):
            await self._throttle()
            try:
                response = await self._get(request_params)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and attempt < 2:
                    retry_after = exc.response.headers.get("Retry-After", "")
                    try:
                        wait_seconds = max(0.2, min(float(retry_after), 30.0))
                    except ValueError:
                        wait_seconds = 1.0
                    self.log.warning("Last.fm rate limited request; retrying in %.1f seconds", wait_seconds)
                    await asyncio.sleep(wait_seconds)
                    continue
                self.log.error("Last.fm HTTP request failed: %s", exc)
                raise LastFmApiError(f"Last.fm HTTP error: {exc.response.status_code}") from exc
            except (httpx.HTTPError, ValueError) as exc:
                self.log.error("Last.fm request failed: %s", exc)
                raise LastFmApiError(f"Last.fm request failed: {exc}") from exc

            if not isinstance(payload, dict):
                raise LastFmApiError("Last.fm returned a non-object JSON response")
            if "error" in payload:
                message = str(payload.get("message") or "unknown Last.fm API error")
                code = payload.get("error")
                self.log.error("Last.fm API error %s: %s", code, message)
                raise LastFmApiError(f"Last.fm API error {code}: {message}")
            self._write_cache(cache_path, payload)
            return payload

        raise LastFmApiError("Last.fm rate limit retry budget was exhausted")

    async def _get(self, params: dict[str, str | int]) -> httpx.Response:
        if self.client is not None:
            return await self.client.get(
                API_URL, params=params, headers={"User-Agent": USER_AGENT}
            )
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            return await client.get(API_URL, params=params)

    async def _throttle(self) -> None:
        async with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_at
            delay = MIN_REQUEST_INTERVAL - elapsed
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_at = time.monotonic()

    def _cache_path(self, method: str, params: dict[str, str | int]) -> Path:
        identity = json.dumps(
            {"method": method, "params": params},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, path: Path) -> dict[str, Any] | None:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            self.log.warning("Ignoring unreadable Last.fm cache %s: %s", path, exc)
            return None
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("cached_at"), (int, float))
            or not isinstance(entry.get("payload"), dict)
        ):
            self.log.warning("Ignoring malformed Last.fm cache %s", path)
            return None
        if time.time() - entry["cached_at"] >= CACHE_TTL_SECONDS:
            return None
        return entry["payload"]

    def _write_cache(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"cached_at": time.time(), "payload": payload},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError as exc:
            self.log.error("Could not write Last.fm cache %s: %s", path, exc)

    @staticmethod
    def _required(value: str, name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{name} must not be empty")
        return normalized

    @staticmethod
    def _limit(value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("limit must be between 1 and 100")
        return value

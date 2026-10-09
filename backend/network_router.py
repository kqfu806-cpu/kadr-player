"""VPN routing — check Last.fm / Deezer / iTunes connectivity each 5 min, log .tools/network.log, GET /api/network/status."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import ROOT

LOG_PATH = ROOT / ".tools" / "network.log"
CHECK_INTERVAL = 5 * 60  # 5 min

logger = logging.getLogger("kadr.network")
logger.setLevel(logging.INFO)
# ensure handler
if not any(getattr(h, "baseFilename", "") and Path(h.baseFilename).name == "network.log" for h in logger.handlers):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False

# current status cache
_status: dict[str, str] = {"lastfm": "unknown", "deezer": "unknown", "itunes": "unknown", "updated_at": ""}
_lock = asyncio.Lock()

async def _check_url(client: httpx.AsyncClient, name: str, url: str, params: dict[str, Any] | None = None) -> str:
    try:
        resp = await client.get(url, params=params, timeout=6.0, follow_redirects=True)
        # Consider ok if status < 500
        if resp.status_code < 500:
            logger.info("%s ok %s", name, resp.status_code)
            return "ok"
        else:
            logger.warning("%s down %s", name, resp.status_code)
            return "down"
    except Exception as exc:
        logger.warning("%s down %s: %s", name, type(exc).__name__, exc)
        return "down"

async def check_once(client: httpx.AsyncClient) -> dict[str, str]:
    tasks = [
        _check_url(client, "lastfm", "https://ws.audioscrobbler.com/2.0/", {"method": "artist.getInfo", "artist": "Cher", "api_key": "demo", "format": "json"}),
        _check_url(client, "deezer", "https://api.deezer.com/search", {"q": "eminem"}),
        _check_url(client, "itunes", "https://itunes.apple.com/search", {"term": "eminem", "limit": "1"}),
    ]
    results = await asyncio.gather(*tasks)
    async with _lock:
        _status["lastfm"] = results[0]
        _status["deezer"] = results[1]
        _status["itunes"] = results[2]
        _status["updated_at"] = datetime.now(timezone.utc).isoformat()
        # also write concise log line
        logger.info("network status lastfm=%s deezer=%s itunes=%s", results[0], results[1], results[2])
        return dict(_status)

async def background_loop(client: httpx.AsyncClient) -> None:
    while True:
        try:
            await check_once(client)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("network check failed")
        try:
            await asyncio.sleep(CHECK_INTERVAL)
        except asyncio.CancelledError:
            raise

def get_status() -> dict[str, str]:
    # return copy
    return dict(_status)

# For immediate sync check without async (used at startup)
def get_status_sync() -> dict[str, str]:
    return dict(_status)

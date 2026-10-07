from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.main as app_module
from backend.lastfm_client import LastFmClient
from backend.lastfm_config import get_lastfm_api_key
from backend.ollama_ai import OllamaStatus


@pytest.mark.asyncio
async def test_client_methods_parse_api_responses_and_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.lastfm_client as lastfm_module

    monkeypatch.setattr(lastfm_module, "MIN_REQUEST_INTERVAL", 0.0)
    methods: list[str] = []
    responses: dict[str, dict[str, Any]] = {
        "artist.getSimilar": {"similarartists": {"artist": [{"name": "Similar"}]}},
        "artist.getTopTracks": {"toptracks": {"track": [{"name": "Top track"}]}},
        "artist.getTopTags": {"toptags": {"tag": [{"name": "rock"}]}},
        "artist.getInfo": {"artist": {"name": "Artist", "bio": {"summary": "Bio"}}},
        "track.getSimilar": {"similartracks": {"track": [{"name": "Related track"}]}},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.params["method"]
        methods.append(method)
        return httpx.Response(200, json=responses[method])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        lastfm = LastFmClient(client=client, cache_dir=tmp_path, api_key="test-key")
        assert await lastfm.get_similar_artists("Artist") == [{"name": "Similar"}]
        assert await lastfm.get_artist_top_tracks("Artist") == [{"name": "Top track"}]
        assert await lastfm.get_artist_top_tags("Artist") == [{"name": "rock"}]
        assert await lastfm.get_artist_info("Artist") == {
            "name": "Artist",
            "bio": {"summary": "Bio"},
        }
        assert await lastfm.get_similar_tracks("Artist", "Title") == [
            {"name": "Related track"}
        ]

    assert len(methods) == 5
    assert len(list(tmp_path.glob("*.json"))) == 5


@pytest.mark.asyncio
async def test_client_uses_fresh_cache_without_api_key(tmp_path) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"similarartists": {"artist": [{"name": "Cached artist"}]}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = LastFmClient(client=client, cache_dir=tmp_path, api_key="test-key")
        result = await first.get_similar_artists("Artist")

    second = LastFmClient(cache_dir=tmp_path, api_key=None)
    assert await second.get_similar_artists("Artist") == result


def test_api_key_is_read_from_env_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# local key\nLASTFM_API_KEY='from-file'\n", encoding="utf-8")
    monkeypatch.setattr("backend.lastfm_config.ENV_FILE", env_file)
    monkeypatch.delenv("LASTFM_API_KEY", raising=False)

    assert get_lastfm_api_key() == "from-file"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def offline_ollama(_client) -> OllamaStatus:
        app_module.ollama.status.online = False
        app_module.ollama.status.error = None
        return app_module.ollama.status

    monkeypatch.setattr(app_module.ollama, "refresh_status", offline_ollama)

    async def similar(_artist: str, _limit: int) -> list[dict[str, Any]]:
        return [{"name": "Similar"}]

    async def top(_artist: str, _limit: int) -> list[dict[str, Any]]:
        return [{"name": "Top track"}]

    async def info(_artist: str) -> dict[str, Any]:
        return {"name": "Artist"}

    monkeypatch.setattr(app_module.lastfm_client, "get_similar_artists", similar)
    monkeypatch.setattr(app_module.lastfm_client, "get_artist_top_tracks", top)
    monkeypatch.setattr(app_module.lastfm_client, "get_artist_info", info)
    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("path", "expected_key"),
    [
        ("/api/lastfm/similar/Artist", "similar"),
        ("/api/lastfm/top/Artist", "tracks"),
        ("/api/lastfm/info/Artist", "artist"),
    ],
)
def test_lastfm_endpoints_return_mocked_data(
    client: TestClient, path: str, expected_key: str
) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert expected_key in response.json()

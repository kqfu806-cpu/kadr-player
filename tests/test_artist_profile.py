from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.artist_discography as discography
import backend.main as app_module
from backend.ollama_ai import OllamaStatus

ARTIST_MBID = "12345678-1234-1234-1234-123456789abc"
RELEASE_MBID = "abcdefab-1234-1234-1234-123456789abc"


@pytest.mark.asyncio
async def test_musicbrainz_release_groups_keep_recent_valid_releases(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discography, "ROOT", tmp_path)
    monkeypatch.setattr(discography, "_throttle", lambda: _no_wait())
    today = date.today()
    entries = [
        {
            "id": RELEASE_MBID,
            "title": "Recent Album",
            "first-release-date": today.isoformat(),
            "primary-type": "Album",
        },
        {
            "id": RELEASE_MBID,
            "title": "Old Album",
            "first-release-date": (today - timedelta(days=180)).isoformat(),
            "primary-type": "Album",
        },
        {
            "id": "bad-id",
            "title": "Malformed ID",
            "first-release-date": today.isoformat(),
        },
    ]

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"release-groups": entries})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discography.fetch_artist_releases(client, ARTIST_MBID)

    assert [release["title"] for release in result] == ["Recent Album"]
    assert result[0]["url"] == f"https://musicbrainz.org/release-group/{RELEASE_MBID}"


async def _no_wait() -> None:
    return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def offline_ollama(_client) -> OllamaStatus:
        app_module.ollama.status.online = False
        app_module.ollama.status.error = None
        return app_module.ollama.status

    monkeypatch.setattr(app_module.ollama, "refresh_status", offline_ollama)

    async def artist_info(name: str) -> dict[str, Any]:
        return {
            "name": name,
            "mbid": ARTIST_MBID,
            "bio": {"summary": "A <a>test</a> bio &amp; facts"},
            "image": [{"#text": "https://lastfm.freetls.fastly.net/image.jpg"}],
        }

    async def similar(_name: str, _limit: int) -> list[dict[str, Any]]:
        return [{"name": "Related Artist", "url": "https://www.last.fm/music/Related"}]

    async def top_tracks(_name: str, _limit: int) -> list[dict[str, Any]]:
        return [
            {
                "name": "Local song",
                "url": "https://www.last.fm/music/Artist/_/Local_song",
                "artist": {"name": "Artist"},
            },
            {
                "name": "Missing song",
                "url": "https://www.last.fm/music/Artist/_/Missing_song",
                "artist": {"name": "Artist"},
            },
        ]

    async def tags(_name: str, _limit: int) -> list[dict[str, Any]]:
        return [{"name": "alternative rock", "count": 20}]

    async def releases(_client, _mbid: str, months: int = 3) -> list[dict[str, Any]]:
        assert months == 3
        return [{"title": "Recent album", "date": "2026-09-01", "url": "https://musicbrainz.org/release-group/id"}]

    monkeypatch.setattr(app_module.lastfm_client, "get_artist_info", artist_info)
    monkeypatch.setattr(app_module.lastfm_client, "get_similar_artists", similar)
    monkeypatch.setattr(app_module.lastfm_client, "get_artist_top_tracks", top_tracks)
    monkeypatch.setattr(app_module.lastfm_client, "get_artist_top_tags", tags)
    monkeypatch.setattr(app_module, "fetch_artist_releases", releases)
    monkeypatch.setattr(
        app_module,
        "library",
        {
            "local-id": SimpleNamespace(
                artist="Artist",
                title="Local song",
                album="Album",
                year="2000",
                id="local-id",
                to_dict=lambda: {
                    "id": "local-id",
                    "artist": "Artist",
                    "title": "Local song",
                    "album": "Album",
                    "year": "2000",
                },
            ),
            "other-id": SimpleNamespace(
                artist="Other",
                title="Not included",
                to_dict=lambda: {"id": "other-id", "artist": "Other", "title": "Not included"},
            ),
        },
    )
    with TestClient(app_module.app) as test_client:
        yield test_client


def test_artist_profile_combines_library_lastfm_and_musicbrainz(
    client: TestClient,
) -> None:
    response = client.get("/api/artist/Artist")

    assert response.status_code == 200
    profile = response.json()
    assert profile["artist"] == "Artist"
    assert profile["bio"] == "A test bio & facts"
    assert profile["genres"] == [{"name": "alternative rock", "count": 20}]
    assert [track["id"] for track in profile["local_tracks"]] == ["local-id"]
    assert profile["top_tracks"][0]["in_library"] is True
    assert profile["top_tracks"][1]["in_library"] is False
    assert profile["similar"][0]["name"] == "Related Artist"
    assert profile["releases"][0]["title"] == "Recent album"


def test_artist_profile_rejects_lastfm_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing_key(_name: str) -> dict[str, Any]:
        from backend.lastfm_client import LastFmConfigurationError

        raise LastFmConfigurationError("LASTFM_API_KEY is required")

    monkeypatch.setattr(app_module.lastfm_client, "get_artist_info", missing_key)

    response = client.get("/api/artist/Artist")

    assert response.status_code == 503
    assert response.json()["detail"] == "LASTFM_API_KEY is required"

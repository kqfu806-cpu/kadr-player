from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

import backend.main as app_module
from backend import weekly
from backend.scanner import Track


def make_track(track_id: str = "local-1") -> Track:
    return Track(
        id=track_id,
        path=f"C:/music/{track_id}.mp3",
        filename=f"{track_id}.mp3",
        artist="Library Artist",
        title="Library Track",
        album="Library Album",
        duration=180,
        year="2024",
        track_no="1",
        has_embedded_cover=False,
        embedded_cover=None,
        bitrate=320,
        tags_ok=True,
    )


@pytest.mark.asyncio
async def test_weekly_separates_and_ranks_singles_and_albums(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(weekly, "CACHE_PATH", tmp_path / "weekly.json")
    monkeypatch.setattr(
        weekly,
        "_artist_seeds",
        _resolved((["Library Artist"], {"new artist": 0.9}, [])),
    )
    monkeypatch.setattr(
        weekly,
        "_lastfm_taste_tracks",
        _resolved({("new artist", "new single")}),
    )
    monkeypatch.setattr(
        weekly,
        "fetch_weekly_releases",
        _resolved({
            "from": date.today().isoformat(),
            "to": date.today().isoformat(),
            "errors": [],
            "tracks": [
                {
                    "artist": "New Artist",
                    "title": "New Single",
                    "date": date.today().isoformat(),
                    "release_type": "single",
                },
                {
                    "artist": "New Artist",
                    "title": "Album Track",
                    "date": date.today().isoformat(),
                    "release_type": "album",
                },
            ],
            "albums": [
                {
                    "artist": "New Artist",
                    "title": "New Album",
                    "date": date.today().isoformat(),
                }
            ],
        }),
    )
    monkeypatch.setattr(
        weekly,
        "_embedding_scores",
        _resolved({("new artist", "new single"): 0.5}),
    )

    result = await weekly.get_weekly(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        [make_track()],
        ollama=object(),  # type: ignore[arg-type]
    )

    assert [item["title"] for item in result["tracks"]] == ["New Single"]
    assert [item["title"] for item in result["albums"]] == ["New Album"]
    assert result["tracks"][0]["relevance"] == 3.3


@pytest.mark.asyncio
async def test_weekly_uses_six_hour_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(weekly, "CACHE_PATH", tmp_path / "weekly.json")
    calls = 0

    async def seeds(*_args: Any) -> tuple[list[str], dict[str, float], list[str]]:
        return ["Library Artist"], {}, []

    async def taste(*_args: Any) -> set[tuple[str, str]]:
        return set()

    async def releases(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"tracks": [], "albums": [], "errors": [], "from": "2025-01-01", "to": "2025-01-07"}

    monkeypatch.setattr(weekly, "_artist_seeds", seeds)
    monkeypatch.setattr(weekly, "_lastfm_taste_tracks", taste)
    monkeypatch.setattr(weekly, "fetch_weekly_releases", releases)
    track = make_track()

    first = await weekly.get_weekly(object(), object(), [track])  # type: ignore[arg-type]
    second = await weekly.get_weekly(object(), object(), [track])  # type: ignore[arg-type]

    assert calls == 1
    assert first == second


def _resolved(value: Any):
    async def result(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return result


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(app_module, "library", {"local-1": make_track()})

    async def offline_ollama(_client) -> Any:
        app_module.ollama.status.online = False
        app_module.ollama.status.error = None
        return app_module.ollama.status

    async def snapshot(*, force: bool = False) -> dict[str, Any]:
        return {
            "source": "test",
            "tracks": [{"title": "Single"}],
            "albums": [{"title": "Album"}],
            "warnings": [],
            "force": force,
        }

    monkeypatch.setattr(app_module.ollama, "refresh_status", offline_ollama)
    monkeypatch.setattr(app_module, "_weekly_snapshot", snapshot)
    with TestClient(app_module.app) as test_client:
        yield test_client


def test_weekly_endpoints_return_their_sections_and_refresh(client: TestClient) -> None:
    tracks = client.get("/api/weekly/tracks")
    albums = client.get("/api/weekly/albums")
    refreshed = client.post("/api/weekly/refresh")

    assert tracks.status_code == 200
    assert tracks.json()["tracks"] == [{"title": "Single"}]
    assert "albums" not in tracks.json()
    assert albums.status_code == 200
    assert albums.json()["albums"] == [{"title": "Album"}]
    assert "tracks" not in albums.json()
    assert refreshed.status_code == 200
    assert refreshed.json()["force"] is True

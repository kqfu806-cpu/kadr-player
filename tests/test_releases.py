from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx
import pytest

from backend import releases


@pytest.mark.asyncio
async def test_weekly_releases_separate_singles_and_albums_and_filter_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def releases_for_artist(*_args: Any, **_kwargs: Any) -> tuple[list[dict[str, Any]], None]:
        return [
            {
                "id": "single-id",
                "artist": "Artist",
                "title": "New Single",
                "date": date.today().isoformat(),
                "url": "https://www.deezer.com/album/1",
                "cover": "https://example.test/cover.jpg",
                "type": "single",
            },
            {
                "id": "album-id",
                "artist": "Artist",
                "title": "New Album",
                "date": date.today().isoformat(),
                "url": "https://www.deezer.com/album/2",
                "cover": "https://example.test/album.jpg",
                "type": "album",
            },
            {
                "id": "existing-album-id",
                "artist": "Artist",
                "title": "Existing Album",
                "date": date.today().isoformat(),
                "url": "https://www.deezer.com/album/3",
                "cover": "https://example.test/existing.jpg",
                "type": "album",
            },
        ], None

    monkeypatch.setattr(releases, "_artist_releases", releases_for_artist)

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "data": [
                    {
                        "title": "Already in library",
                        "link": "https://www.deezer.com/track/old",
                        "preview": "https://cdns-preview-3.dzcdn.net/old.mp3",
                    },
                    {
                        "title": "New Track",
                        "link": "https://www.deezer.com/track/new",
                        "preview": "https://cdns-preview-3.dzcdn.net/new.mp3",
                    },
                ]
            }

    class Client:
        async def get(self, *_args: Any, **_kwargs: Any) -> Response:
            return Response()

    result = await releases.fetch_weekly_releases(
        Client(),  # type: ignore[arg-type]
        ["Artist"],
        local_tracks=[
            {
                "artist": "artist",
                "title": "already in library",
                "album": "Existing Album",
            }
        ],
    )

    assert [item["title"] for item in result["tracks"]] == ["New Track"]
    assert result["tracks"][0]["preview"].endswith("new.mp3")
    assert [item["title"] for item in result["albums"]] == ["New Album"]
    assert all("id" not in item for item in result["albums"])


@pytest.mark.asyncio
async def test_weekly_releases_degrade_when_deezer_is_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class DownClient:
        async def get(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            request = httpx.Request("GET", "https://api.deezer.com/search/artist")
            raise httpx.ConnectError("offline", request=request)

    result = await releases.fetch_weekly_releases(
        DownClient(),  # type: ignore[arg-type]
        ["Artist"],
    )

    assert result["tracks"] == []
    assert result["albums"] == []
    assert len(result["errors"]) == 1
    assert "Artist" in result["errors"][0]
    assert any(record.name == "kadr.deezer" for record in caplog.records)


@pytest.mark.asyncio
async def test_weekly_releases_include_all_artists_and_support_thirty_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artist_names = [f"Artist {index}" for index in range(20)]
    looked_up: list[str] = []
    observed_cutoffs: list[date] = []
    tracklist_lookups: list[str] = []

    async def artist_releases(
        _client: Any,
        name: str,
        cutoff: date,
        _today: date,
        _semaphore: Any,
    ) -> tuple[list[dict[str, Any]], None]:
        looked_up.append(name)
        observed_cutoffs.append(cutoff)
        return [
            {
                "id": f"album-{name}",
                "artist": name,
                "title": "Album",
                "date": date.today().isoformat(),
                "url": "https://www.deezer.com/album/1",
                "cover": "",
                "type": "album",
            },
            {
                "id": f"single-{name}",
                "artist": name,
                "title": "Single",
                "date": date.today().isoformat(),
                "url": "https://www.deezer.com/album/2",
                "cover": "",
                "type": "single",
            },
        ], None

    async def single_tracks(
        _client: Any, release: dict[str, Any], _semaphore: Any
    ) -> list[dict[str, Any]]:
        tracklist_lookups.append(release["type"])
        return []

    monkeypatch.setattr(releases, "_artist_releases", artist_releases)
    monkeypatch.setattr(releases, "_single_tracks", single_tracks)

    result = await releases.fetch_weekly_releases(
        object(),  # type: ignore[arg-type]
        artist_names,
        days=30,
    )

    assert set(looked_up) == set(artist_names)
    assert set(observed_cutoffs) == {date.today() - timedelta(days=30)}
    assert tracklist_lookups == ["single"] * len(artist_names)
    assert len(result["albums"]) == len(artist_names)

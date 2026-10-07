from __future__ import annotations

from datetime import date
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

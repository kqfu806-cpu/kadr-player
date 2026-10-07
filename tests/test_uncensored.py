from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import backend.main as app_module
import backend.censorship_detector as detector
from backend.scanner import Track
from backend.censorship_detector import classify_track
from backend.uncensored_finder import _rank
from backend.ollama_ai import OllamaStatus
from backend.uncensored_replacer import _clean_tag, validate_youtube_url


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def offline_ollama(_client: Any) -> OllamaStatus:
        app_module.ollama.status.online = False
        app_module.ollama.status.error = None
        for name in app_module.ollama.status.models:
            app_module.ollama.status.models[name] = False
        return app_module.ollama.status

    monkeypatch.setattr(app_module.ollama, "refresh_status", offline_ollama)
    with TestClient(app_module.app) as test_client:
        yield test_client


def make_track(
    *,
    title: str = "Track",
    duration: float = 245,
    bitrate: int = 320,
    path: str = "C:/music/track.mp3",
) -> Track:
    return Track(
        id="track-id",
        path=path,
        filename=Path(path).name,
        artist="Artist",
        title=title,
        album="Album",
        duration=duration,
        year="2020",
        track_no="1",
        has_embedded_cover=False,
        embedded_cover=None,
        bitrate=bitrate,
        tags_ok=True,
    )


def test_detector_marks_clean_keyword() -> None:
    suspect = classify_track(make_track(title="Track (clean)"), 245)

    assert suspect is not None
    assert suspect["reason"] == "keyword"
    assert "keyword" in suspect["reasons"]


def test_detector_marks_track_shorter_than_canonical_by_over_ten_seconds() -> None:
    suspect = classify_track(make_track(duration=220), 245)

    assert suspect is not None
    assert suspect["reason"] == "short"
    assert suspect["canonical_duration"] == 245
    assert suspect["local_duration"] == 220


def test_detector_marks_low_bitrate() -> None:
    suspect = classify_track(make_track(bitrate=127), 245)

    assert suspect is not None
    assert "lowbitrate" in suspect["reasons"]


def test_candidate_requires_unedited_title_and_official_artist_channel() -> None:
    track = make_track(title="Song")
    official = {
        "title": "Artist - Song (Official Audio)",
        "channel": "Artist",
        "duration": 245,
        "webpage_url": "https://www.youtube.com/watch?v=abc",
    }
    edited = {**official, "title": "Artist - Song (Clean) Official Audio"}
    unrelated = {**official, "channel": "Unrelated Official Channel"}
    unrelated_song = {**official, "title": "Artist - Different Song"}
    alternate_version = {**official, "title": "Artist - Song (Piano Version)"}

    assert _rank(official, track, 245) is not None
    assert _rank(edited, track, 245) is None
    assert _rank(unrelated, track, 245) is None
    assert _rank(unrelated_song, track, 245) is None
    assert _rank(alternate_version, track, 245) is None


def test_candidate_title_allows_a_longer_official_title() -> None:
    track = make_track(title="в своей ванной")
    candidate = {
        "title": "Я утонул в своей ванной",
        "channel": "Artist",
        "duration": 168,
        "webpage_url": "https://www.youtube.com/watch?v=abc",
    }

    assert _rank(candidate, track, 168) is not None


@pytest.mark.asyncio
async def test_detector_batches_musicbrainz_lookups_and_reports_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = make_track(title="Track (clean)", duration=220, path=str(tmp_path / "a.mp3"))
    monkeypatch.setattr(detector, "SUSPECTS_PATH", tmp_path / "suspects.json")
    monkeypatch.setattr(detector, "DURATION_CACHE_PATH", tmp_path / "durations.json")
    monkeypatch.setattr(detector, "_mb_last_request", 0.0)

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "recordings": [
                    {
                        "title": "Track",
                        "length": 245000,
                        "artist-credit": [{"name": "Artist"}],
                    }
                ]
            }

    class Client:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def get(self, url: str, **kwargs: Any) -> Response:
            self.calls.append({"url": url, **kwargs})
            return Response()

    progress: list[tuple[int, int, int]] = []
    client = Client()
    results = await detector.detect_suspects(
        [track], client, lambda done, total, suspects: progress.append((done, total, suspects))
    )

    assert len(client.calls) == 1
    assert "recording:" in client.calls[0]["params"]["query"]
    assert results[0]["reason"] == "keyword"
    assert results[0]["reasons"] == ["keyword", "short"]
    assert results[0]["canonical_duration"] == 245
    assert progress[-1] == (1, 1, 1)


@pytest.mark.asyncio
async def test_duration_batch_uses_exact_queries_for_every_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detector, "_mb_last_request", 0.0)

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(detector.asyncio, "sleep", no_wait)

    class Response:
        def __init__(self, title: str) -> None:
            self.title = title

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "recordings": [
                    {
                        "title": self.title,
                        "length": 180000,
                        "artist-credit": [{"name": "Artist"}],
                    }
                ]
            }

    class Client:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def get(self, _url: str, **kwargs: Any) -> Response:
            query = kwargs["params"]["query"]
            self.queries.append(query)
            title = "Song A" if 'recording:"Song A"' in query else "Song B"
            return Response(title)

    client = Client()
    matches = await detector._lookup_duration_batch(
        client, [("Artist", "Song A"), ("Artist", "Song B")]
    )

    assert matches == {"artist|song a": 180, "artist|song b": 180}
    assert len(client.queries) == 2
    assert all(" OR " not in query for query in client.queries)


def test_replacer_cleans_edit_markers_from_tags() -> None:
    assert _clean_tag("Song Title (clean)") == "Song Title"
    assert _clean_tag("Album - Radio Edit") == "Album"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
        "https://music.youtube.com/watch?v=abc123",
    ],
)
def test_replacer_accepts_youtube_hosts(url: str) -> None:
    assert validate_youtube_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com.attacker.example/watch?v=abc",
        "https://example.com/watch?v=abc",
        "http://youtube.com/watch?v=abc",
    ],
)
def test_replacer_rejects_non_youtube_hosts(url: str) -> None:
    with pytest.raises(ValueError):
        validate_youtube_url(url)


def test_uncensored_api_endpoints_return_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(app_module, "_uncensored_log", Mock())
    track = make_track(path=str(tmp_path / "track.mp3"))
    monkeypatch.setattr(app_module, "library", {track.id: track})
    monkeypatch.setattr(app_module, "current_folder", str(tmp_path))
    monkeypatch.setattr(
        app_module,
        "uncensored_scan_state",
        {
            "status": "complete",
            "processed": 1,
            "total": 1,
            "suspects": 1,
            "items": [
                {
                    "id": track.id,
                    "path": track.path,
                    "reason": "keyword",
                    "canonical_duration": 245,
                    "candidates": [],
                }
            ],
            "by_reason": {"keyword": 1},
            "error": None,
        },
    )
    monkeypatch.setattr(
        app_module,
        "uncensored_replace_state",
        {"status": "idle", "processed": 0, "total": 0, "results": [], "error": None},
    )

    async def detect(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def candidates(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def replace(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "old_path": track.path,
            "new_path": str(tmp_path / "track.opus"),
            "archive_path": str(tmp_path / "archive" / "track.mp3"),
            "duration": 245,
            "size": 1024,
        }

    monkeypatch.setattr(app_module, "detect_suspects", detect)
    monkeypatch.setattr(app_module, "find_candidates", candidates)
    monkeypatch.setattr(app_module, "replace_track", replace)
    monkeypatch.setattr(app_module.cache, "delete", lambda _track_id: None)
    monkeypatch.setattr(app_module.track_index, "delete", lambda _track_id: None)
    monkeypatch.setattr(app_module.track_index, "upsert", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(app_module, "read_track", lambda _path: make_track(path=str(tmp_path / "track.opus")))

    assert client.get("/api/uncensored/status").status_code == 200
    assert client.get(f"/api/uncensored/candidates/{track.id}").status_code == 200
    assert client.post(
        f"/api/uncensored/replace/{track.id}",
        json={"confirmed": True, "candidate_url": "https://youtu.be/abc123"},
    ).status_code == 200
    assert client.post(
        "/api/uncensored/replace_all", json={"confirmed": True}
    ).status_code == 200

    scan_response = client.get("/api/uncensored/scan")
    assert scan_response.status_code == 200

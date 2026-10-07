from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import backend.main as app_module
from backend import stats
from backend.lastfm_client import LastFmConfigurationError
from backend.ollama_ai import OllamaStatus


def test_record_play_is_idempotent_and_requires_more_than_30_seconds(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "stats.sqlite3"

    with pytest.raises(ValueError, match="exceed 30"):
        stats.record_play("track", "session", 30, "Artist", "Title", db_path)

    first = stats.record_play("track", "session", 31, "Artist", "Title", db_path)
    second = stats.record_play("track", "session", 80, "Artist", "Title", db_path)
    assert first["id"] == second["id"]
    assert second["duration"] == 80
    assert stats.summary(db_path)["all"] == {"tracks": 1, "minutes": 1.3}

    with pytest.raises(ValueError, match="different track"):
        stats.record_play("another-track", "session", 90, "Other", "Title", db_path)


def test_top_and_timeline_return_daily_and_hourly_buckets(tmp_path: Path) -> None:
    db_path = tmp_path / "stats.sqlite3"
    first = stats.record_play("id-1", "session-1", 60, "Artist", "Song", db_path)
    stats.record_play("id-2", "session-2", 90, "Artist", "Other", db_path)
    stats.record_play("id-3", "session-3", 50, "Other", "Tune", db_path)

    top = stats.top("all", db_path=db_path)
    timeline = stats.timeline(30, db_path)

    assert top["artists"][0]["artist"] == "Artist"
    assert top["artists"][0]["plays"] == 2
    assert top["tracks"][0]["title"] == "Other"
    assert len(timeline["days"]) == 30
    assert len(timeline["hours"]) == 24
    assert sum(hour["plays"] for hour in timeline["hours"]) == 3
    assert first["timestamp"].startswith(datetime.now(timezone.utc).date().isoformat())


def test_timeline_excludes_old_hour_buckets(tmp_path: Path) -> None:
    db_path = tmp_path / "stats.sqlite3"
    old = (datetime.now(timezone.utc) - timedelta(days=50)).isoformat()
    with stats._connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO plays (session_id, track_id, timestamp, duration, artist, title)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("old-session", "old-track", old, 60, "Old Artist", "Old Track"),
        )

    assert sum(hour["plays"] for hour in stats.timeline(30, db_path)["hours"]) == 0


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    db_path = tmp_path / "api-stats.sqlite3"
    monkeypatch.setattr(stats, "STATS_DB_PATH", db_path)
    monkeypatch.setattr(
        app_module,
        "library",
        {
            "track-1": SimpleNamespace(
                id="track-1", artist="Artist", title="Song", duration=200
            )
        },
    )

    async def offline_ollama(_client) -> OllamaStatus:
        app_module.ollama.status.online = False
        app_module.ollama.status.error = None
        return app_module.ollama.status

    monkeypatch.setattr(app_module.ollama, "refresh_status", offline_ollama)

    async def no_tags(_artist: str, _limit: int = 10) -> list[dict[str, Any]]:
        raise LastFmConfigurationError("missing Last.fm key")

    monkeypatch.setattr(app_module.lastfm_client, "get_artist_top_tags", no_tags)
    with TestClient(app_module.app) as test_client:
        yield test_client


def test_stats_endpoints_and_play_validation(client: TestClient) -> None:
    assert client.get("/api/stats/summary").json()["all"] == {
        "tracks": 0,
        "minutes": 0.0,
    }
    assert client.get("/api/stats/timeline").status_code == 200
    assert client.get("/api/stats/top?period=nope").status_code == 422
    assert client.post(
        "/api/stats/play",
        json={"track_id": "track-1", "session_id": "short", "duration": 30},
    ).status_code == 400

    saved = client.post(
        "/api/stats/play",
        json={"track_id": "track-1", "session_id": "listen-1", "duration": 31},
    )
    assert saved.status_code == 200
    assert saved.json()["play"]["artist"] == "Artist"
    assert client.get("/api/stats/summary").json()["all"]["tracks"] == 1
    assert client.get("/api/stats/top?period=all").json()["tracks"][0]["title"] == "Song"
    assert client.get("/api/stats/genres").json()["warnings"]


def test_csv_export_escapes_spreadsheet_formulas(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        app_module.listening_stats,
        "csv_rows",
        lambda: [
            {
                "track_id": "track-1",
                "timestamp": "2026-10-07T10:00:00+00:00",
                "duration": 60,
                "artist": "=1+1",
                "title": "@SUM(A1:A2)",
            }
        ],
    )

    response = client.get("/api/stats/export.csv")

    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[1][3] == "'=1+1"
    assert rows[1][4] == "'@SUM(A1:A2)"

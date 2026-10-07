from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import backend.main as app_module
from backend import wave
from backend.ollama_ai import OllamaStatus


def make_track(track_id: str, artist: str = "Artist", title: str | None = None):
    return SimpleNamespace(
        id=track_id,
        artist=artist,
        title=title or f"Song {track_id}",
        album="Album",
        to_dict=lambda: {
            "id": track_id,
            "artist": artist,
            "title": title or f"Song {track_id}",
        },
    )


def test_embeddings_round_trip_and_cosine_similarity(tmp_path: Path) -> None:
    db_path = tmp_path / "wave.sqlite3"
    wave.store_embedding("a", [1.0, 0.0], db_path)
    wave.store_embedding("b", [0.0, 1.0], db_path)

    embeddings = wave.load_embeddings(db_path)

    assert embeddings == {"a": [1.0, 0.0], "b": [0.0, 1.0]}
    assert wave.cosine_similarity(embeddings["a"], embeddings["a"]) == pytest.approx(1)
    assert wave.cosine_similarity(embeddings["a"], embeddings["b"]) == pytest.approx(0)


@pytest.mark.asyncio
async def test_ensure_embeddings_only_generates_missing_tracks(tmp_path: Path) -> None:
    db_path = tmp_path / "wave.sqlite3"
    wave.store_embedding("first", [1.0, 0.0], db_path)
    calls: list[str] = []

    class Ollama:
        status = SimpleNamespace(models={"nomic-embed-text": True})

        async def embed(self, _client, text: str) -> list[float]:
            calls.append(text)
            return [0.0, 1.0]

    tracks = [make_track("first"), make_track("second")]
    computed = await wave.ensure_embeddings(
        tracks, object(), Ollama(), db_path=db_path  # type: ignore[arg-type]
    )

    assert computed == 1
    assert len(calls) == 1
    assert set(wave.load_embeddings(db_path)) == {"first", "second"}


@pytest.mark.asyncio
async def test_ensure_embeddings_batches_missing_tracks_and_reuses_cache(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "wave.sqlite3"
    wave.store_embedding("cached", [1.0, 0.0], db_path)

    class Ollama:
        status = SimpleNamespace(models={"nomic-embed-text": True})
        batches: list[list[str]] = []

        async def embed_batch(self, _client, texts: list[str]) -> list[list[float]]:
            self.batches.append(texts)
            return [[0.0, 1.0] for _ in texts]

        async def embed(self, _client, _text: str) -> list[float]:
            raise AssertionError("single embeddings should not be used when batch succeeds")

    ollama = Ollama()
    tracks = [make_track("cached"), make_track("new-a"), make_track("new-b")]

    computed = await wave.ensure_embeddings(
        tracks, object(), ollama, db_path=db_path  # type: ignore[arg-type]
    )
    repeated = await wave.ensure_embeddings(
        tracks, object(), ollama, db_path=db_path  # type: ignore[arg-type]
    )

    assert computed == 2
    assert repeated == 0
    assert len(ollama.batches) == 1
    assert len(ollama.batches[0]) == 2
    assert set(wave.load_embeddings(db_path)) == {"cached", "new-a", "new-b"}


def test_signal_weights_and_skip_penalizes_nearest_tracks(tmp_path: Path) -> None:
    db_path = tmp_path / "wave.sqlite3"
    wave.store_embedding("skipped", [1.0, 0.0], db_path)
    wave.store_embedding("similar", [0.9, 0.1], db_path)
    wave.store_embedding("unrelated", [0.0, 1.0], db_path)

    result = wave.record_signal("skipped", "skip", db_path)
    wave.record_signal("liked", "like", db_path)
    wave.record_signal("disliked", "dislike", db_path)

    assert result["weight"] == -1
    assert result["similar_tracks_updated"] == 2
    with wave._connect(db_path) as connection:
        rows = connection.execute(
            "SELECT track_id, signal, weight FROM wave_signals"
        ).fetchall()
    weights = {(row["track_id"], row["signal"]): row["weight"] for row in rows}
    assert weights[("liked", "like")] == 3
    assert weights[("disliked", "dislike")] == -5
    assert weights[("similar", "similar_skip")] == pytest.approx(-0.5 * wave.cosine_similarity([1, 0], [0.9, 0.1]))


@pytest.mark.asyncio
async def test_recommendations_mix_familiar_and_discovery_tracks(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "wave.sqlite3"
    tracks = [make_track(f"track-{index}", artist=f"Artist {index % 4}") for index in range(12)]
    wave.store_embedding("track-0", [1.0, 0.0], db_path)
    wave.store_embedding("track-1", [0.95, 0.05], db_path)
    wave.store_embedding("track-2", [0.9, 0.1], db_path)
    wave.record_signal("track-0", "like", db_path)
    play = {
        "session_id": "session-0",
        "track_id": "track-0",
        "timestamp": "2026-10-01T12:00:00+00:00",
        "duration": 120,
        "artist": "Artist 0",
        "title": "Song track-0",
    }
    with wave._connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO plays (session_id, track_id, timestamp, duration, artist, title)
            VALUES (:session_id, :track_id, :timestamp, :duration, :artist, :title)
            """,
            play,
        )

    result = await wave.recommend(tracks, count=10, db_path=db_path, mode="library")

    assert len(result["items"]) == 10
    assert len({item["id"] for item in result["items"]}) == 10
    assert any(item["id"] == "track-0" for item in result["items"])
    assert sum(1 for item in result["items"] if item["reason"] in {"редко слушал", "новый исполнитель"}) >= 4
    assert result["period"] in {"утро", "день", "вечер", "ночь"}


@pytest.mark.asyncio
async def test_new_wave_returns_only_non_library_tracks_with_safe_preview(
    tmp_path: Path,
) -> None:
    local_tracks = [make_track("local", artist="Library Artist", title="Known Song")]

    class LastFm:
        async def get_similar_artists(self, _artist: str, _limit: int) -> list[dict[str, str]]:
            return [{"name": "Similar Artist"}]

        async def get_artist_top_tracks(
            self, _artist: str, _limit: int
        ) -> list[dict[str, str]]:
            return [{"name": "New Song", "url": "https://www.last.fm/music/Similar"}]

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "data": [
                    {
                        "artist": {"name": "Similar Artist"},
                        "preview": "https://cdns-preview-1.dzcdn.net/preview.mp3",
                        "album": {"cover_medium": "https://cdn.example.test/cover.jpg"},
                        "link": "https://www.deezer.com/track/1",
                    }
                ]
            }

    class Deezer:
        async def get(self, *_args, **_kwargs) -> Response:
            return Response()

    result = await wave.recommend(
        local_tracks,
        count=5,
        lastfm=LastFm(),  # type: ignore[arg-type]
        client=Deezer(),  # type: ignore[arg-type]
        db_path=tmp_path / "wave.sqlite3",
        mode="new",
    )

    assert len(result["items"]) == 1
    assert result["items"][0]["external"] is True
    assert result["items"][0]["title"] == "New Song"
    assert result["items"][0]["preview"].endswith("preview.mp3")
    assert result["items"][0]["id"] != "local"


@pytest.mark.asyncio
async def test_favorite_and_forgotten_modes_filter_library(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "wave.sqlite3"
    tracks = [make_track(f"track-{index}") for index in range(3)]
    now = datetime.now(timezone.utc).isoformat()
    with wave._connect(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO plays (session_id, track_id, timestamp, duration, artist, title)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (f"played-{index}", "track-0", now, 120, "Artist", "Song track-0")
                for index in range(3)
            ],
        )
    wave.record_signal("track-0", "like", db_path)

    favorite = await wave.recommend(tracks, mode="favorite", db_path=db_path)
    forgotten = await wave.recommend(tracks, mode="forgotten", db_path=db_path)

    assert [item["id"] for item in favorite["items"]] == ["track-0"]
    assert {item["id"] for item in forgotten["items"]} == {"track-1", "track-2"}


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(wave, "STATS_DB_PATH", tmp_path / "api-wave.sqlite3")
    monkeypatch.setattr(app_module, "library", {"track-1": make_track("track-1")})

    async def offline_ollama(_client) -> OllamaStatus:
        app_module.ollama.status.online = False
        app_module.ollama.status.error = None
        return app_module.ollama.status

    async def no_warmup() -> None:
        return None

    async def recommendations(*_args, **_kwargs) -> dict[str, Any]:
        return {
            "items": [make_track("track-1").to_dict() | {"reason": "редко слушал"}],
            "mood": "",
            "period": "вечер",
        }

    monkeypatch.setattr(app_module.ollama, "refresh_status", offline_ollama)
    monkeypatch.setattr(app_module, "_start_wave_embedding_warmup", no_warmup)
    monkeypatch.setattr(wave, "recommend", recommendations)
    with TestClient(app_module.app) as test_client:
        yield test_client


def test_wave_endpoints_validate_and_save_feedback(client: TestClient) -> None:
    queued = client.get("/api/wave/queue?count=1")
    assert queued.status_code == 200
    assert queued.json()["items"][0]["id"] == "track-1"
    assert client.get("/api/wave/next").json()["track"]["id"] == "track-1"

    assert client.post(
        "/api/wave/signal", json={"track_id": "track-1", "signal": "skip", "duration": 31}
    ).status_code == 400
    assert client.post(
        "/api/wave/signal", json={"track_id": "track-1", "signal": "unknown"}
    ).status_code == 400
    liked = client.post(
        "/api/wave/signal", json={"track_id": "track-1", "signal": "like"}
    )
    assert liked.status_code == 200
    assert liked.json()["weight"] == 3


@pytest.mark.asyncio
async def test_embedding_warmup_schedules_daily_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track("cached")
    monkeypatch.setattr(app_module, "library", {"cached": track})
    monkeypatch.setattr(app_module, "wave_embedding_maintenance_task", None)
    monkeypatch.setattr(
        app_module.wave_engine, "load_embeddings", lambda: {"cached": [1.0]}
    )

    await app_module._start_wave_embedding_warmup()
    task = app_module.wave_embedding_maintenance_task

    assert task is not None
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

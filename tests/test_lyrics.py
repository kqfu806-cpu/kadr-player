from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend import lyrics
from backend import main as app_module
from backend.scanner import Track


def make_track(
    path: Path,
    *,
    artist: str = "Artist",
    title: str = "Song Title",
    album: str = "Album",
    duration: float = 80,
) -> Track:
    return Track(
        id="track-id",
        path=str(path),
        filename=path.name,
        artist=artist,
        title=title,
        album=album,
        duration=duration,
        year="2020",
        track_no="1",
        has_embedded_cover=False,
        embedded_cover=None,
        bitrate=320,
        tags_ok=True,
    )


def test_lyrics_cache_key_includes_track_and_normalized_metadata(tmp_path: Path) -> None:
    original = make_track(tmp_path / "one.mp3")
    duplicate = replace(
        original,
        id="other-id",
        path=str(tmp_path / "two.mp3"),
        filename="two.mp3",
        artist=" artist ",
        title="SONG   TITLE",
        album="ALBUM",
    )
    different_duration = replace(original, duration=81)

    assert lyrics.lyrics_cache_key(original) != lyrics.lyrics_cache_key(duplicate)
    assert lyrics.lyrics_cache_key(original) != lyrics.lyrics_cache_key(different_duration)
    assert len(lyrics.lyrics_cache_key(original)) == 64


def test_plain_lyrics_remain_unsynchronized() -> None:
    payload = lyrics._pack(
        "lrclib-plain",
        lyrics._plain_lines("one\ntwo three\nfour"),
        synced=False,
    )

    timed = lyrics._normalize_lyrics_timing(payload)

    assert timed["synced"] is False
    assert timed["timing"] == "none"
    assert all(line["t"] is None for line in timed["lines"])
    assert all(not line["words"] for line in timed["lines"])


@pytest.mark.asyncio
async def test_fetch_lyrics_caches_by_hash_and_uses_cache_on_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = make_track(tmp_path / "song.mp3")
    monkeypatch.setattr(lyrics, "LYRICS_DIR", tmp_path / "lyrics")
    monkeypatch.setattr(lyrics, "_local_lrc", lambda _track: None)
    calls = 0

    async def lyrics_from_lrclib(_client: Any, _track: Track) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return lyrics._pack(
            "lrclib-plain",
            lyrics._plain_lines("one\ntwo three\nfour"),
            synced=False,
        )

    async def unexpected_provider(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("A cached result should prevent another provider request")

    monkeypatch.setattr(lyrics, "_lrclib_get", lyrics_from_lrclib)
    monkeypatch.setattr(lyrics, "_lrclib_search_pick", unexpected_provider)
    monkeypatch.setattr(lyrics, "_mirror", unexpected_provider)
    monkeypatch.setattr(lyrics, "_ollama_recall", unexpected_provider)
    ollama = SimpleNamespace(status=SimpleNamespace(models={}))

    first = await lyrics.fetch_lyrics(object(), ollama, track)
    second = await lyrics.fetch_lyrics(object(), ollama, track)

    assert calls == 1
    assert first == second
    assert second["cache_key"] == lyrics.lyrics_cache_key(track)
    assert second["timing"] == "none"
    assert lyrics.lyrics_cache_path(track).is_file()


@pytest.mark.asyncio
async def test_lyrics_api_indexes_hashed_cache_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = make_track(tmp_path / "song.mp3")
    key = lyrics.lyrics_cache_key(track)
    indexed: list[tuple[str, dict[str, Any]]] = []

    async def cached_result(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "cache_key": key, "source": "lrclib"}

    monkeypatch.setattr(app_module, "library", {track.id: track})
    monkeypatch.setattr(app_module, "_client", lambda: object())
    monkeypatch.setattr(app_module, "fetch_lyrics", cached_result)
    monkeypatch.setattr(
        app_module,
        "lyrics_cache_path",
        lambda _track: app_module.ROOT / "cache" / "lyrics" / f"{key}.json",
    )
    monkeypatch.setattr(
        app_module.track_index,
        "upsert",
        lambda track_id, **values: indexed.append((track_id, values)),
    )

    result = await app_module.lyrics(track.id)

    assert result["cache_key"] == key
    assert indexed == [
        (
            track.id,
            {"lyrics": f"cache/lyrics/{key}.json", "lyrics_source": "lrclib"},
        )
    ]

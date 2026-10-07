from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from backend.audio_fetcher import AudioFetcher


def test_missing_binary_logs_and_returns_false(tmp_path: Path, caplog) -> None:
    fetcher = AudioFetcher(binary_path=tmp_path / "missing.exe")

    assert fetcher.fetch("https://example.test/audio", tmp_path / "track.opus") is False
    assert "executable is missing" in caplog.text


def test_fetch_invokes_local_binary_with_expected_arguments(
    tmp_path: Path, monkeypatch
) -> None:
    binary = tmp_path / "audiodl.exe"
    binary.write_bytes(b"mock binary")
    output = tmp_path / "track.opus"
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("backend.audio_fetcher.subprocess.run", fake_run)
    fetcher = AudioFetcher(binary_path=binary)

    assert fetcher.fetch("https://example.test/audio", output) is True
    assert captured["command"] == [
        str(binary),
        "-x",
        "--audio-format",
        "opus",
        "-o",
        str(output),
        "https://example.test/audio",
    ]
    assert captured["kwargs"]["check"] is False


def test_nonzero_exit_logs_and_returns_false(tmp_path: Path, monkeypatch, caplog) -> None:
    binary = tmp_path / "audiodl.exe"
    binary.write_bytes(b"mock binary")
    monkeypatch.setattr(
        "backend.audio_fetcher.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2, stderr="download failed"
        ),
    )
    fetcher = AudioFetcher(binary_path=binary)

    assert fetcher.fetch("https://example.test/audio", tmp_path / "track.opus") is False
    assert "download failed" in caplog.text


def test_subprocess_error_logs_and_returns_false(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    binary = tmp_path / "audiodl.exe"
    binary.write_bytes(b"mock binary")

    def fail_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("audiodl.exe", timeout=300)

    monkeypatch.setattr("backend.audio_fetcher.subprocess.run", fail_run)
    fetcher = AudioFetcher(binary_path=binary)

    assert fetcher.fetch("https://example.test/audio", tmp_path / "track.opus") is False
    assert "Audio fetch failed" in caplog.text

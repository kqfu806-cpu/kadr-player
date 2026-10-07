from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as app_module
from backend.error_logging import configure_error_logging
from backend.ollama_ai import OllamaStatus


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def offline_ollama(_client) -> OllamaStatus:
        app_module.ollama.status.online = False
        app_module.ollama.status.error = "Ollama недоступна"
        for model in app_module.ollama.status.models:
            app_module.ollama.status.models[model] = False
        return app_module.ollama.status

    monkeypatch.setattr(app_module.ollama, "refresh_status", offline_ollama)
    with TestClient(app_module.app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_sqlite_lock_returns_retryable_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def locked_database() -> dict[str, object]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app_module.listening_stats, "summary", locked_database)
    caplog.set_level(logging.ERROR, logger="kadr.errors")

    response = client.get("/api/stats/summary")

    assert response.status_code == 503
    assert response.json()["detail"] == "База данных занята, повторите запрос"
    assert any(record.name == "kadr.errors" for record in caplog.records)


def test_unexpected_error_returns_safe_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_summary() -> dict[str, object]:
        raise RuntimeError("internal-only detail")

    monkeypatch.setattr(app_module.listening_stats, "summary", failed_summary)

    response = client.get("/api/stats/summary")

    assert response.status_code == 500
    assert "cache/errors.log" in response.json()["detail"]
    assert "internal-only detail" not in response.text


def test_error_log_rotates_and_filters_other_loggers(tmp_path: Path) -> None:
    log_path = tmp_path / "errors.log"
    handler = configure_error_logging(log_path, max_bytes=150, backup_count=1)
    root = logging.getLogger()
    logger = logging.getLogger("kadr.resilience_test")
    unrelated = logging.getLogger("unrelated.resilience_test")
    try:
        logger.warning("before-rotation")
        unrelated.warning("not-a-backend-error")
        logger.warning("x" * 300)
        handler.flush()

        rotated = Path(f"{log_path}.1")
        assert rotated.exists()
        assert "before-rotation" in rotated.read_text(encoding="utf-8")
        assert "not-a-backend-error" not in log_path.read_text(encoding="utf-8")
    finally:
        root.removeHandler(handler)
        handler.close()

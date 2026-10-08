from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as app_module
from backend.ollama_ai import OllamaStatus


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def offline_ollama(_client) -> OllamaStatus:
        app_module.ollama.status.online = False
        app_module.ollama.status.error = None
        for name in app_module.ollama.status.models:
            app_module.ollama.status.models[name] = False
        return app_module.ollama.status

    monkeypatch.setattr(app_module.ollama, "refresh_status", offline_ollama)
    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api/health",
        "/api/artists",
        "/api/library",
        "/api/fs/roots",
        "/api/cache",
        "/manifest.json",
        "/sw.js",
        "/static/i18n.js",
    ],
)
def test_smoke_endpoints_return_success(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200


def test_unicode_music_folder_is_default_and_persists_after_restart(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "Музыка"
    folder.mkdir()
    monkeypatch.setattr(app_module, "APP_SETTINGS_DB_PATH", tmp_path / "settings.sqlite3")
    monkeypatch.setattr(app_module, "DEFAULT_MUSIC_FOLDER", folder)
    monkeypatch.setattr(app_module, "current_folder", None)
    monkeypatch.setattr(app_module, "library", {})

    assert client.get("/api/health").json()["folder"] == str(folder)

    scanned = client.post("/api/scan", json={"path": str(folder)})
    assert scanned.status_code == 200
    assert scanned.json()["folder"] == str(folder.resolve())

    monkeypatch.setattr(app_module, "current_folder", None)
    assert client.get("/api/health").json()["folder"] == str(folder.resolve())


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
    ],
)
def test_cors_allows_loopback_http_origins(client: TestClient, origin: str) -> None:
    response = client.options(
        "/api/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://localhost:8000",
        "http://localhost.attacker.example",
        "http://192.168.1.10:8000",
    ],
)
def test_cors_rejects_non_loopback_origins(client: TestClient, origin: str) -> None:
    response = client.options(
        "/api/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )

    assert "access-control-allow-origin" not in response.headers

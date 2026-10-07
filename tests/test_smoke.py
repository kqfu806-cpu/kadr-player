from collections.abc import Iterator

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
    ],
)
def test_smoke_endpoints_return_success(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200


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

# Testing Patterns

## 1) Test Stack and Commands

- Primary test framework: pytest (installed in project venv via `requirements-dev.txt`).
- Assertion/mocking tools: pytest fixtures/monkeypatch; FastAPI `TestClient` via Starlette/httpx.
- Commands:

```text
Run tests: venv\Scripts\python.exe -m pytest
Focused tests: venv\Scripts\python.exe -m pytest tests
Static Python syntax check: python -m compileall -q backend
```

## 2) Test Layout

- Tests live in root `tests/` and use `test_*.py`.
- `tests/test_smoke.py` provides a fixture that patches Ollama status refresh and starts the app through `TestClient` lifespan.
- `tests/test_ollama_context.py` verifies generation, vision and embedding requests include their configured `num_ctx`.
- `tests/test_lyrics.py` verifies metadata-hash cache identity, cached provider reuse, estimated line timing and hashed cache indexing.
- `tests/test_uncensored.py` covers keyword/duration/bitrate classification, YouTube URL validation, and uncensored API endpoints with mocked downloads.

## 3) Test Scope Matrix

| Scope | Covered? | Typical target | Notes |
|---|---|---|---|
| Unit | Partial | Ollama request context options; lyrics cache/timing; suspect classification and URL validation | Scanner/resolver unit coverage remains limited |
| Integration | Partial | FastAPI routes with mocked Ollama health and replacement; CORS preflight behavior | External MusicBrainz/YouTube calls and GPU inference are not exercised |
| E2E | No | Browser playback and folder selection | Not automated |

## 4) Mocking and Isolation Strategy

- Main mocking approach: monkeypatch shared Ollama status refresh for route tests; fake HTTP responses for Ollama request-shape tests.
- Isolation guarantees: each `TestClient` context runs app startup/shutdown; request data is local, with no external API call in tested routes.
- Coverage: CORS accepts HTTP localhost/127.0.0.1 origins on arbitrary ports and rejects non-loopback origins.

## 5) Coverage and Quality Signals

- Coverage tool/threshold: [TODO: none configured].
- Current coverage: [TODO: not measured].
- Quality gap: scanner, media resolution, external provider behavior and browser playback have no checked-in automated regression tests.

## 6) Evidence

- `requirements.txt`
- `requirements-dev.txt`
- `tests/test_smoke.py`
- `tests/test_ollama_context.py`
- `tests/test_lyrics.py`
- `tests/test_uncensored.py`
- `launch.py`

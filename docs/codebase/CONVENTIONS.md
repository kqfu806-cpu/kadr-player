# Coding Conventions

## 1) Naming Rules

| Item | Rule | Example | Evidence |
|---|---|---|---|
| Python files | lowercase snake_case | `media_resolver.py` | `backend/` |
| Python functions/variables | snake_case | `scan_folder`, `track_id` | `backend/scanner.py` |
| Python types | PascalCase | `Track`, `OllamaClient` | `backend/scanner.py`, `backend/ollama_ai.py` |
| Constants | uppercase snake_case | `OLLAMA_URL`, `AUDIO_EXTENSIONS` | `backend/config.py` |
| JavaScript functions/variables | camelCase | `playIndex`, `coverCache` | `frontend/app.js` |

## 2) Formatting and Linting

- Formatter: [TODO: no formatter config found].
- Linter: [TODO: no linter config found].
- Python sources use type annotations in core modules and UTF-8 docstrings/comments.
- Run commands: `python -m compileall -q backend` and `venv\Scripts\python.exe -m pytest`; no configured lint command.

## 3) Import and Module Conventions

- Python standard-library imports precede external imports and local package imports in inspected modules.
- Local package imports are relative (`from .config import ...`).
- No barrel-export pattern is used; `backend/__init__.py` contains only a package docstring.

## 4) Error and Logging Conventions

- FastAPI endpoints use `HTTPException` for client-facing invalid/not-found conditions.
- Provider modules log failures and often return `None`/fallback results; startup and server output use Python logging/print.
- No central sensitive-data redaction convention was found: [TODO].

## 5) Testing Conventions

- Test location/naming: root `tests/`, files use `test_*.py`.
- Mocking strategy: pytest fixtures/monkeypatch for app lifecycle and fake HTTP responses for Ollama request-shape tests.
- Coverage expectation: [TODO: none configured].

## 6) Evidence

- `backend/main.py`
- `backend/scanner.py`
- `backend/config.py`
- `frontend/app.js`
- `requirements.txt`
- `requirements-dev.txt`
- `tests/test_smoke.py`
- `tests/test_ollama_context.py`

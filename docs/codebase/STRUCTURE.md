# Codebase Structure

## 1) Top-Level Map

| Path | Purpose | Evidence |
|---|---|---|
| `backend/` | FastAPI routes and media/business logic | `backend/main.py`, module docstrings |
| `backend/censorship_detector.py` | Metadata, bitrate, and MusicBrainz duration checks | `backend/censorship_detector.py` |
| `backend/uncensored_finder.py`, `backend/uncensored_replacer.py` | Candidate discovery and confirmed, archived Opus replacement | respective modules |
| `frontend/` | Static PWA client assets | `frontend/index.html`, `frontend/app.js`, `frontend/sw.js` |
| `music_demo/` | Two demo WAV files and instructions | `music_demo/ЧИТАЙ.txt` |
| `tests/` | FastAPI/CORS smoke, Ollama context, and uncensored workflow tests | `tests/` |
| `cache/` | Runtime metadata, images, logs, browser app profiles | `backend/config.py`, `launch.py` |
| `.tools/` | Hardware/project/Ollama reports | generated reports |
| `launch.py`, `run.bat`, `run.sh` | Cross-platform startup helpers | file contents |
| `requirements.txt` | Python dependencies | manifest |
| `requirements-dev.txt` | Runtime requirements plus pytest tooling | manifest |

## 2) Entry Points

- Main Windows launcher: `run.bat` → `launch.py`.
- Backend module entry: `python -m backend` → `backend/__main__.py` → `backend/main.py:run`.
- Uvicorn target: `backend.main:app`, started by `launch.py`.
- Secondary helper: `python -m backend.tray` for system tray.
- Frontend entry: `/` serves `frontend/index.html`; it loads `/static/app.js` and `/static/styles.css`.

## 3) Module Boundaries

| Boundary | Responsibility | Keep separate |
|---|---|---|
| `backend/main.py` | API composition, app lifecycle, current in-memory library | Feature-specific parsing/providers |
| `backend/scanner.py` | Track metadata and filesystem scanning | HTTP routes and UI |
| Other `backend/*.py` | Feature logic and integrations | Browser DOM |
| `frontend/` | User interface and browser state | Direct local filesystem access outside API |
| `cache/` | Generated application state | Source code |

## 4) Naming and Organization Rules

- Python modules use lowercase `snake_case.py`; functions/variables are snake_case, classes use PascalCase.
- Frontend source uses lowercase filenames (`app.js`, `styles.css`); JavaScript identifiers are camelCase.
- Backend is organized as a single Python package by capability, not a multi-package workspace.
- Python imports use relative package imports, e.g. `from .config import ...`.

## 5) Evidence

- `run.bat`
- `launch.py`
- `backend/__main__.py`
- `backend/main.py`
- `frontend/index.html`
- `backend/config.py`
- `backend/censorship_detector.py`
- `backend/uncensored_finder.py`
- `backend/uncensored_replacer.py`

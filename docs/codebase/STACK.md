# Technology Stack

## 1) Runtime Summary

| Area | Value | Evidence |
|---|---|---|
| Primary language | Python backend; vanilla JavaScript, HTML and CSS frontend | `backend/`, `frontend/` |
| Runtime + version | Active project venv Python 3.13.14; launcher and README specify 3.10+ | `launch.py`, `README.md`, runtime output |
| Package manager | pip in project venv | `launch.py`, `requirements.txt` |
| Module/build system | Python package `backend`; static frontend, no build step or npm manifest | `backend/__main__.py`, `frontend/` |

## 2) Production Frameworks and Dependencies

Versions are lower bounds from `requirements.txt`; no lockfile is present.

| Dependency | Declared version | Role |
|---|---:|---|
| fastapi | >=0.115.0 | HTTP/WebSocket API |
| uvicorn[standard] | >=0.32.0 | ASGI server |
| httpx | >=0.27.0 | Async external/local HTTP |
| mutagen | >=1.47.0 | Audio metadata |
| yt-dlp | >=2025.1.15 | Search YouTube candidates and download user-confirmed audio replacements |
| Pillow | >=10.4.0 | Cover image processing |
| python-multipart | >=0.0.12 | Multipart support |
| aiofiles | >=24.1.0 | Async file support |
| websockets | >=13.0 | WebSocket runtime support |
| beautifulsoup4 | >=4.12.0 | Lyrics HTML parsing |
| rapidfuzz | >=3.9.0 | Lyrics/candidate text matching |
| pystray | >=0.19.0 | System tray |
| plyer | >=2.1.0 | Notifications |

## 3) Development Toolchain

| Tool | Purpose | Evidence |
|---|---|---|
| Python launcher / pip | Environment and dependency setup | `launch.py`, `run.bat` |
| pytest / pytest-asyncio | FastAPI smoke/CORS and Ollama context-option tests | `requirements-dev.txt`, `tests/` |
| Formatter/linter | [TODO: no configuration found] | project root |

## 4) Key Commands

```text
run.bat
python launch.py
python -m backend
python -m compileall -q backend
python -m pytest
```

There is no configured lint or frontend build command.

## 5) Environment and Config

- Configuration is in `backend/config.py`; launcher sets `PYTHONUTF8`, `PYTHONIOENCODING`, and defaults `OLLAMA_NUM_GPU`.
- Required environment variables: none documented.
- Runtime constraints: server binds to `127.0.0.1:8000`; local Ollama uses `127.0.0.1:11434`.

## 6) Evidence

- `requirements.txt`
- `launch.py`
- `backend/config.py`
- `README.md`
- `backend/main.py`
- `requirements-dev.txt`
- `backend/uncensored_finder.py`
- `backend/uncensored_replacer.py`

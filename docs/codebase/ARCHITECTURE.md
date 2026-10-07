# Architecture

## 1) Architectural Style

- **Style:** Single-process local client/server app, with a capability-oriented backend package and static browser frontend.
- **Evidence:** FastAPI routes compose helpers in `backend/main.py`; static UI is served from `frontend/`.
- **Constraints:** binds to loopback; uses local disk for audio/cache; Ollama is local and optional for degraded heuristic operation.

## 2) System Flow

```text
run.bat -> launch.py -> Uvicorn/backend.main:app -> REST/WebSocket -> scanner/resolver/integration modules -> disk/Ollama/public APIs -> frontend
```

1. `launch.py` checks Python, creates the project venv, installs dependencies, checks Ollama, and runs Uvicorn.
2. FastAPI startup creates a shared async `httpx.AsyncClient` and refreshes Ollama status.
3. Frontend submits a folder to `/api/scan`; `scanner.scan_folder` reads audio metadata and returns track data.
4. `/api/resolve` invokes `MediaResolver`, which searches YouTube, ranks candidates and asks Ollama when available; cache/index persist results.
5. Frontend plays local audio via `/api/stream` or embeds a YouTube clip; related endpoints retrieve covers, lyrics, artists, and releases.
6. The quality scan checks tags/bitrate and looks up recording lengths through MusicBrainz; candidate discovery ranks official YouTube results, and replacement downloads only after confirmation, validates duration/size, archives the prior file, then refreshes library/cache/index state.
7. `/api/lyrics/{track_id}` checks a local `.lrc`, LRCLIB and lyric mirrors before the optional local Ollama fallback; results are cached by a SHA-256 hash of normalized artist/title/album/duration metadata. Timestamped lyrics follow their source timings; plain lyrics receive explicitly estimated line timings from track duration.
8. The weekly release panel checks up to 12 followed/library artists via Deezer, splits track previews from album releases, excludes local artist/title and artist/album pairs, and links externally rather than downloading media.

## 3) Layer/Module Responsibilities

| Module | Owns | Must not own | Evidence |
|---|---|---|---|
| `backend/main.py` | HTTP/WebSocket routes, lifecycle, in-memory library | Provider-specific parsing | `backend/main.py` |
| `backend/scanner.py` | Filesystem traversal, metadata and Track model | Browser presentation | `backend/scanner.py` |
| `backend/media_resolver.py` | Resolve/cache media decision and validate clip | UI rendering | `backend/media_resolver.py` |
| `backend/ollama_ai.py` | Ollama client, embedding/LLM/vision and heuristics | HTTP route registration | `backend/ollama_ai.py` |
| `backend/censorship_detector.py` | Suspect rules and cached canonical recording durations | File replacement | `backend/censorship_detector.py` |
| `backend/uncensored_finder.py`, `backend/uncensored_replacer.py` | Official-result search and guarded audio replacement | Unconfirmed implicit replacement | respective modules |
| `backend/covers.py`, `lyrics.py`, `releases.py` | External catalogue and text providers | Process startup | feature modules |
| `frontend/app.js` | UI state and browser playback | Direct filesystem traversal | `frontend/app.js` |

## 4) Reused Patterns

| Pattern | Where found | Why |
|---|---|---|
| Shared service instances | `backend/main.py` globals | Share cache, HTTP client, Ollama status |
| Provider fallback/cascade | `backend/media_resolver.py`, `backend/covers.py`, `backend/lyrics.py` | Return usable results when providers fail |
| Persistent JSON store | `backend/cache_store.py`, `backend/index_store.py` | Reuse results across runs |
| Async fan-out | `backend/main.py:Hub`, `backend/releases.py` | Push progress and concurrently query artist releases |

## 5) Known Architectural Risks

- `backend/main.py` combines lifecycle, routes and process-global library state, increasing change coupling.
- The UI is a large single `frontend/app.js`; module-level state and handlers are not isolated.
- `backend/main.py` accepts browser CORS origins on HTTP localhost/127.0.0.1 at any port while exposing local filesystem browsing/streaming endpoints; loopback binding limits network exposure but is not an origin-level authorization policy.

## 6) Evidence

- `run.bat`, `launch.py`
- `backend/main.py`, `backend/media_resolver.py`
- `backend/scanner.py`, `backend/config.py`
- `frontend/index.html`, `frontend/app.js`
- `backend/censorship_detector.py`, `backend/uncensored_finder.py`, `backend/uncensored_replacer.py`

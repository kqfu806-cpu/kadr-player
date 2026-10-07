# Codebase Concerns

## 1) Top Risks (Prioritized)

| Severity | Concern | Evidence | Impact | Suggested action |
|---|---|---|---|---|
| Medium | Filesystem browsing/streaming endpoints are accessible cross-origin to localhost/127.0.0.1 HTTP origins on any port | `backend/main.py`; `tests/test_smoke.py` | A local web page can attempt requests to the player API; CORS is an origin boundary, not authentication | Keep loopback-only binding; consider origin/token protection if exposing sensitive operations |
| Medium | Automated tests cover endpoint availability/CORS, but not scanner, resolver or browser playback behavior | `tests/test_smoke.py` | Core media workflows may regress without detection | Add isolated scanner/resolver tests and browser-flow coverage |
| Medium | Track duration/title/channel matching can select a wrong version despite the official-channel and duration filters | `backend/uncensored_finder.py`, `backend/uncensored_replacer.py` | Incorrect audio could replace the intended recording | Review the selected candidate before single-track replacement; retain the archive |

## 2) Technical Debt

| Debt item | Why it exists | Where | Risk if ignored | Suggested fix |
|---|---|---|---|---|
| Large mixed-responsibility API module | Routes and lifecycle grew together | `backend/main.py` (521 lines) | Changes to unrelated endpoints become coupled | Extract routers by feature when adding behavior |
| Large client script | UI state, API access, playback and rendering share one closure | `frontend/app.js` (2075 lines) | Harder to isolate and test UI changes | Split by feature without changing behavior |
| Dependency ranges not locked | Requirements use lower bounds | `requirements.txt` | Rebuilds may resolve newer dependency versions | Add a reproducible lock strategy |

## 3) Security Concerns

| Risk | Category | Evidence | Current mitigation | Gap |
|---|---|---|---|---|
| Local API exposes filesystem browsing, audio streaming, and shutdown/cache operations to browser origins on localhost/127.0.0.1 | Access control | `backend/main.py` (`allow_origin_regex`, `/api/fs/*`, `/api/shutdown`); `tests/test_smoke.py` | CORS rejects non-loopback origins; Uvicorn binds to `127.0.0.1` | CORS does not authenticate local origins; no request token |

This is a static code observation, not a penetration test.

## 4) Performance and Scaling Concerns

| Concern | Evidence | Current symptom | Scaling risk | Suggested improvement |
|---|---|---|---|---|
| Recursive full-folder scan reads metadata for each matching audio file | `backend/scanner.py:scan_folder` | Scan latency grows with library size | Slow response and synchronous filesystem work in API handler | Measure on large libraries; move scans to background with progress if needed |
| Artists embeddings are fetched sequentially for up to 96 artists | `backend/artists.py:build_artists` | Initial artist load may wait on many model calls | High first-load latency | Batch or bounded-concurrent embeddings and persist cache |
| Plain lyrics use estimated line timing when providers return no timestamps | `backend/lyrics.py`, `frontend/app.js` | Highlighting is visibly approximate and labeled as such | Estimated lines can drift from vocal timing | Prefer timestamped LRC when available |
| In-memory library is process-local | `backend/main.py` | Library must be selected again after restart | Not suited to multiple workers | Persist selected library state if persistence is a product requirement |

## 5) Fragile/High-Churn Areas

| Area | Why fragile | Churn signal | Safe change strategy |
|---|---|---|---|
| `backend/main.py` | Many routes and lifecycle/global state in one module | Git history unavailable; churn [TODO] | Add endpoint tests before extracting routers |
| `frontend/app.js` | Large stateful single-file UI | Git history unavailable; churn [TODO] | Make small changes and verify key playback flows |
| `backend/ollama_ai.py` | Several model modes and heuristic fallbacks | Git history unavailable; churn [TODO] | Test with mocked Ollama responses and offline mode |

## 6) Intent Confirmation

- Prior intent disabled iTunes for the normal cover workflow; the new user-requested replacement feature uses iTunes Search for replacement artwork and falls back to Deezer (`backend/uncensored_replacer.py`).

## 7) Evidence

- `backend/main.py`
- `backend/scanner.py`
- `backend/artists.py`
- `backend/covers.py`
- `launch.py`
- `README.md`
- `requirements.txt`
- `backend/uncensored_finder.py`
- `backend/uncensored_replacer.py`

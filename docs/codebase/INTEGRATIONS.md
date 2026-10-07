# External Integrations

## 1) Integration Inventory

| System | Type | Purpose | Auth | Criticality | Evidence |
|---|---|---|---|---|---|
| Ollama | Local HTTP API | LLM, embeddings, image understanding | Local service; no app key | Medium; heuristic fallbacks | `backend/config.py`, `backend/ollama_ai.py` |
| YouTube | Search/embedded player | Find and play candidate music videos | Public web client; no developer key | Medium | `backend/youtube_search.py`, `frontend/app.js` |
| yt-dlp / YouTube | Subprocess CLI | Search official audio candidates and retrieve a candidate after explicit confirmation | YouTube URL host allowlist; no app key | Medium | `backend/uncensored_finder.py`, `backend/uncensored_replacer.py` |
| MusicBrainz | Public recording API | Canonical-duration lookup for the quality scan | User-Agent; rate limited to at most one request per second | Low | `backend/censorship_detector.py`, `backend/config.py` |
| Deezer | Public catalogue API | Album covers, artist release metadata, and official short previews | No credentials in code | Low/medium | `backend/covers.py`, `backend/releases.py` |
| MusicBrainz / Cover Art Archive | Public catalogue APIs | Release lookup and cover art | User-Agent configured | Low | `backend/covers.py`, `backend/config.py` |
| iTunes Search API | Public artwork API | Replacement-only Opus cover lookup; Deezer fallback | No credentials in code | Low | `backend/uncensored_replacer.py`, `backend/covers.py` |
| LRCLIB and lyric HTML providers | Public HTTP | Lyrics | No credentials in code | Low | `backend/lyrics.py` |
| Browser local storage / Cache API | Client-side stores | Preferences, favorites, offline shell assets | Browser origin storage | Low | `frontend/app.js`, `frontend/sw.js` |

## 2) Data Stores

| Store | Role | Access layer | Key risk |
|---|---|---|---|
| `cache/matches.json` | Clip/resolution decisions | `backend/cache_store.py` | JSON corruption is treated as empty cache |
| `cache/index.json` | Compact media metadata | `backend/index_store.py` | Local single-process file |
| `cache/lyrics/<sha256>.json` | Positive and time-limited negative lyric lookup cache; key hashes normalized artist/title/album/duration | `backend/lyrics.py` | Disk growth/retention policy not documented |
| Browser local storage | Favorites, followed artists, and six-hour weekly-release response cache | `frontend/app.js` | Browser storage can be cleared independently |
| `cache/covers/`, `cache/artists.json` | Generated/downloaded assets and lookup cache | feature modules | Disk growth/retention policy not documented |
| In-memory `library` | Current scanned track list | `backend/main.py` | Lost when server restarts |

## 3) Secrets and Credentials Handling

- No API keys/secrets or environment templates were found as requirements; services are used without credentials.
- YouTube web client key is present in `backend/youtube_search.py` and documented there as public client configuration, not a developer secret.
- Credential rotation: not applicable to observed integrations; [TODO] if authenticated providers are introduced.

## 4) Reliability and Failure Behavior

- Timeouts are set per provider in `backend/config.py` and feature modules.
- Several providers retry LRCLIB failures; other providers use skip/fallback behavior.
- Ollama is optional: launcher and backend check status; resolver uses heuristics when models are unavailable.
- No circuit breaker is configured.

## 5) Observability for Integrations

- Provider and status logging is present in `backend/covers.py`, `backend/lyrics.py`, `backend/main.py`, and `launch.py`.
- Metrics/tracing: [TODO: none found].
- Failures are not uniformly surfaced to the user; several helpers return `None` or fallback assets.
- Replacement downloads are capped at 50 MiB; originals are moved to `archive/censored/` and replacement events are logged under `.tools/`.

## 6) Evidence

- `backend/config.py`
- `backend/ollama_ai.py`
- `backend/youtube_search.py`
- `backend/covers.py`
- `backend/lyrics.py`
- `backend/releases.py`
- `frontend/sw.js`
- `backend/censorship_detector.py`
- `backend/uncensored_finder.py`
- `backend/uncensored_replacer.py`

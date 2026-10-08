"""Конфигурация приложения Курымдык."""

from pathlib import Path

# Корень проекта (на уровень выше пакета backend)
ROOT = Path(__file__).resolve().parent.parent

FRONTEND_DIR = ROOT / "frontend"
CACHE_DIR = ROOT / "cache"
COVERS_DIR = CACHE_DIR / "covers"
EMBEDDED_DIR = CACHE_DIR / "embedded"
LYRICS_DIR = CACHE_DIR / "lyrics"
MATCHES_PATH = CACHE_DIR / "matches.json"
INDEX_PATH = CACHE_DIR / "index.json"
STATS_DB_PATH = CACHE_DIR / "stats.sqlite3"
APP_SETTINGS_DB_PATH = CACHE_DIR / "app_settings.sqlite3"
DEFAULT_MUSIC_FOLDER = Path.home() / "Music" / "Музыка"
LOG_DIR = CACHE_DIR / "logs"

HOST = "127.0.0.1"
PORT = 8000

# Локальная Ollama — никаких облачных ИИ
OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_LLM = "deepseek-r1:8b"
OLLAMA_VISION = "llava:7b"
OLLAMA_EMBED = "nomic-embed-text"
OLLAMA_LLM_NUM_CTX = 4096
OLLAMA_VISION_NUM_CTX = 4096
OLLAMA_EMBED_NUM_CTX = 2048

# Таймауты запросов к моделям (секунды)
OLLAMA_EMBED_TIMEOUT = 12.0
OLLAMA_LLM_TIMEOUT = 45.0
OLLAMA_VISION_TIMEOUT = 45.0
OLLAMA_TAGS_TIMEOUT = 2.0
OLLAMA_LYRICS_TIMEOUT = 20.0
OLLAMA_NUM_GPU = 99

# Внешние API (без ключей)
ITUNES_SEARCH = "https://itunes.apple.com/search"
DEEZER_SEARCH = "https://api.deezer.com/search"
MUSICBRAINZ_RELEASE = "https://musicbrainz.org/ws/2/release/"
COVERART_ARCHIVE = "https://coverartarchive.org/release"

# MusicBrainz отдаёт 403 без внятного UA
USER_AGENT = "KADR/1.0 ( mailto:kadr@local )"
COVER_TIMEOUT = 4.0

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".m4a", ".mp4", ".ogg", ".opus",
    ".aac", ".wma", ".aiff", ".aif", ".ape", ".wv", ".oga",
}

# Клип длиннее этого (и длиннее самого трека с запасом) — скорее всего концерт/фильм
MAX_CLIP_DURATION = 15 * 60

# Сколько кандидатов с YouTube берём в работу
YOUTUBE_CANDIDATES = 10
YOUTUBE_TOP_FOR_LLM = 5

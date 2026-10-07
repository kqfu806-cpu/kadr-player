"""Сканирование локальной музыкальной папки и чтение ID3/тегов."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile

from .config import AUDIO_EXTENSIONS, EMBEDDED_DIR

# «01. Artist - Title», «Artist – Title», «Artist — Title»
_RE_NUM_ARTIST_TITLE = re.compile(
    r"^\s*\d{1,3}\s*[-.)]\s*(.+?)\s*[-–—]\s+(.+?)\s*$"
)
_RE_ARTIST_TITLE = re.compile(r"^\s*(.+?)\s*[-–—]\s+(.+?)\s*$")


def track_id_for(path: str) -> str:
    """Стабильный id по абсолютному пути (не зависит от тегов)."""
    norm = str(Path(path).resolve()).replace("\\", "/").lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _first(tags: Any, *keys: str) -> str:
    if not tags:
        return ""
    for key in keys:
        val = tags.get(key)
        if not val:
            continue
        if isinstance(val, list):
            val = val[0] if val else ""
        text = str(val).strip()
        if text:
            return text
    return ""


def parse_filename(stem: str) -> tuple[str, str]:
    """Достаём артиста и название из «грязного» имени файла."""
    name = stem
    # Убираем типичный мусор из раздач
    name = re.sub(r"[\[(][^\])]*[\])]", " ", name)
    name = re.sub(
        r"\b(320|256|192|128)\s?kbps\b|\b(web|cd|vinyl)\s?rip\b|\bfretless\b",
        " ",
        name,
        flags=re.I,
    )
    name = re.sub(r"\s+", " ", name).strip(" ._")

    m = _RE_NUM_ARTIST_TITLE.match(name)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = _RE_ARTIST_TITLE.match(name)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", name


def _extract_embedded_cover(audio: Any, tid: str) -> str | None:
    """Сохраняем встроенную обложку (APIC / covr), если она есть."""
    EMBEDDED_DIR.mkdir(parents=True, exist_ok=True)
    data = None
    mime = "image/jpeg"

    try:
        tags = getattr(audio, "tags", None)
        if not tags:
            return None

        # ID3
        if hasattr(tags, "getall"):
            apics = tags.getall("APIC") or []
            if apics:
                data = apics[0].data
                mime = getattr(apics[0], "mime", "") or mime

        # MP4
        if data is None and "covr" in tags:
            covr = tags["covr"]
            if covr:
                data = bytes(covr[0])

        # FLAC / Vorbis pictures
        if data is None and hasattr(audio, "pictures") and audio.pictures:
            data = audio.pictures[0].data
            mime = audio.pictures[0].mime or mime
    except Exception:
        return None

    if not data:
        return None

    ext = ".png" if "png" in mime.lower() else ".jpg"
    out = EMBEDDED_DIR / f"{tid}{ext}"
    try:
        out.write_bytes(data)
        return str(out)
    except OSError:
        return None


@dataclass
class Track:
    id: str
    path: str
    filename: str
    artist: str
    title: str
    album: str
    duration: float
    year: str
    track_no: str
    has_embedded_cover: bool
    embedded_cover: str | None
    bitrate: int
    tags_ok: bool  # True, если artist+title взяты из тегов, не из имени файла

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def read_track(path: Path) -> Track | None:
    """Читаем один аудиофайл. Возвращаем None, если файл не читается."""
    try:
        audio = MutagenFile(str(path), easy=True)
    except Exception:
        audio = None

    duration = 0.0
    bitrate = 0
    artist = title = album = year = track_no = ""
    tags_ok = False

    if audio is not None:
        info = getattr(audio, "info", None)
        if info is not None:
            duration = float(getattr(info, "length", 0) or 0)
            br = getattr(info, "bitrate", 0) or 0
            bitrate = int(br) if br < 1_000_000 else int(br)  # уже в bps или kbps
            if bitrate > 4000:  # явно бит/с
                bitrate = bitrate // 1000

        artist = _first(audio, "artist", "albumartist", "performer")
        title = _first(audio, "title")
        album = _first(audio, "album")
        year = _first(audio, "date", "year")[:4]
        track_no = _first(audio, "tracknumber", "track")
        if artist and title:
            tags_ok = True

    if not title or not artist:
        fa, ft = parse_filename(path.stem)
        if not artist:
            artist = fa or path.parent.name or "Неизвестный исполнитель"
        if not title:
            title = ft or path.stem

    # Год из имени папки «1998 - Album» как запасной вариант
    if not album:
        album = path.parent.name

    tid = track_id_for(str(path))

    embedded = None
    has_cover = False
    try:
        raw = MutagenFile(str(path))
        embedded = _extract_embedded_cover(raw, tid)
        has_cover = embedded is not None
    except Exception:
        pass

    return Track(
        id=tid,
        path=str(path.resolve()),
        filename=path.name,
        artist=artist.strip(),
        title=title.strip(),
        album=str(album).strip(),
        duration=duration,
        year=year,
        track_no=str(track_no).split("/")[0].strip(),
        has_embedded_cover=has_cover,
        embedded_cover=embedded,
        bitrate=bitrate,
        tags_ok=tags_ok,
    )


def scan_folder(folder: str) -> list[Track]:
    """Рекурсивно сканируем папку, сортируем по альбому/номеру/имени."""
    root = Path(folder)
    if not root.is_dir():
        raise FileNotFoundError(f"Папка не найдена: {folder}")

    tracks: list[Track] = []
    for p in root.rglob("*"):
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        if p.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        # пропускаем скрытые / системные
        if p.name.startswith(".") or p.name.startswith("._"):
            continue
        tr = read_track(p)
        if tr:
            tracks.append(tr)

    def sort_key(t: Track) -> tuple:
        try:
            num = int(re.sub(r"\D", "", t.track_no) or 0)
        except ValueError:
            num = 0
        return (t.album.lower(), num, t.filename.lower())

    tracks.sort(key=sort_key)
    return tracks

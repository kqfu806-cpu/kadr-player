"""Replace a local track with a confirmed, rights-cleared YouTube candidate."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from mutagen import File as MutagenFile
from mutagen.flac import Picture
from PIL import Image

from .config import ROOT
from .covers import search_deezer, search_itunes
from .scanner import Track

log = logging.getLogger("kadr.uncensored")
MAX_BYTES = 50 * 1024 * 1024
ARCHIVE_DIR = ROOT / "archive" / "censored"
_EDIT_MARKERS = re.compile(
    r"\b(?:clean|radio[\s-]+edit|edited|censored|version)\b",
    re.IGNORECASE,
)


def _clean_tag(value: str) -> str:
    normalized = re.sub(r"\s+", " ", _EDIT_MARKERS.sub(" ", value)).strip(" -_()[]")
    return normalized or value


def validate_youtube_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not (
        host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")
    ):
        raise ValueError("Разрешены только HTTPS URL с youtube.com или youtu.be")
    if not parsed.path:
        raise ValueError("Некорректный URL YouTube")
    return url


def _download(url: str, workdir: Path) -> Path:
    output = workdir / "replacement.%(ext)s"
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "--max-filesize",
        "50M",
        "-x",
        "--audio-format",
        "opus",
        "--audio-quality",
        "0",
        "-o",
        str(output),
        url,
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-2000:] or "yt-dlp download failed")
    files = [path for path in workdir.glob("replacement.*") if path.is_file()]
    if not files:
        raise RuntimeError("yt-dlp completed without producing an audio file")
    downloaded = files[0]
    if downloaded.stat().st_size > MAX_BYTES:
        raise ValueError("Загруженный файл превышает ограничение 50 МБ")
    return downloaded


def _tag_opus(path: Path, track: Track) -> None:
    audio = MutagenFile(path, easy=True)
    if audio is None:
        raise RuntimeError("Mutagen не распознал загруженный Opus-файл")
    audio["artist"] = track.artist
    audio["title"] = _clean_tag(track.title)
    if track.album:
        audio["album"] = _clean_tag(track.album)
    if track.year:
        audio["date"] = track.year
    if track.track_no:
        audio["tracknumber"] = track.track_no
    audio.save()


async def _cover(client: httpx.AsyncClient, track: Track) -> bytes | None:
    url, _ = await search_itunes(client, track.artist, track.title, track.album)
    if not url:
        url, _ = await search_deezer(client, track.artist, track.title, track.album)
    if not url:
        return None
    response = await client.get(url, timeout=12, follow_redirects=True)
    response.raise_for_status()
    if len(response.content) > 5 * 1024 * 1024:
        raise ValueError("Обложка превышает ограничение 5 МБ")
    return response.content


def _embed_cover(path: Path, data: bytes) -> None:
    with Image.open(BytesIO(data)) as image:
        image_format = image.format
    if image_format not in {"JPEG", "PNG"}:
        raise ValueError(f"Неподдерживаемый формат обложки: {image_format}")
    picture = Picture()
    picture.data = data
    picture.mime = "image/png" if image_format == "PNG" else "image/jpeg"
    picture.type = 3
    picture.desc = "Cover"
    audio = MutagenFile(path)
    if audio is None or audio.tags is None:
        raise RuntimeError("Не удалось открыть Opus для добавления обложки")
    audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
    audio.save()


def _archive_path(source: Path, music_root: Path) -> Path:
    source_resolved = source.resolve()
    root_resolved = music_root.resolve()
    try:
        relative = source_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("Трек находится вне текущей музыкальной папки") from exc
    destination = (ARCHIVE_DIR / relative).resolve()
    if destination == ARCHIVE_DIR.resolve() or ARCHIVE_DIR.resolve() not in destination.parents:
        raise ValueError("Некорректный путь архива")
    if destination.exists():
        destination = destination.with_name(
            f"{destination.stem}-{uuid.uuid4().hex[:8]}{destination.suffix}"
        )
    return destination


async def replace_track(
    track: Track,
    candidate_url: str,
    canonical_duration: int | None,
    music_root: Path,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    url = validate_youtube_url(candidate_url)
    if not canonical_duration or canonical_duration <= 0:
        raise ValueError("Нет проверенной канонической длительности; замена отменена")
    source = Path(track.path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Исходный файл не найден: {source}")
    destination = source.with_suffix(".opus")
    if destination.exists() and destination != source:
        raise FileExistsError(f"Файл назначения уже существует: {destination}")

    workdir = Path(tempfile.mkdtemp(prefix=".kadr-uncensored-", dir=source.parent))
    archive = _archive_path(source, music_root)
    archived = False
    installed = False
    try:
        downloaded = await asyncio.to_thread(_download, url, workdir)
        audio = MutagenFile(downloaded)
        duration = float(getattr(getattr(audio, "info", None), "length", 0) or 0)
        if not duration or abs(duration - canonical_duration) / canonical_duration > 0.03:
            raise ValueError("Длительность загруженного файла отличается от канонической более чем на 3%")
        _tag_opus(downloaded, track)
        try:
            cover = await _cover(client, track)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Cover lookup failed for %s: %s", track.id, exc)
            cover = None
        if cover:
            try:
                _embed_cover(downloaded, cover)
            except (OSError, ValueError) as exc:
                log.warning("Cover embedding failed for %s: %s", track.id, exc)
        if downloaded.stat().st_size > MAX_BYTES:
            raise ValueError("Подготовленный файл превышает ограничение 50 МБ")
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(archive))
        archived = True
        shutil.move(str(downloaded), str(destination))
        installed = True
        return {
            "old_path": str(source),
            "new_path": str(destination),
            "archive_path": str(archive),
            "duration": duration,
            "size": destination.stat().st_size,
        }
    except Exception:
        if installed and destination.exists():
            destination.unlink()
        if archived and archive.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(archive), str(source))
        raise
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, True)

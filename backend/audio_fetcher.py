"""Audio downloader — yt-dlp renamed to tools/audiodl.exe with tagging and cover handling."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx

from .config import ROOT

AUDIODL_PATH = ROOT / "tools" / "audiodl.exe"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def _logger() -> logging.Logger:
    logger = logging.getLogger("kadr.audio_fetcher")
    logger.setLevel(logging.INFO)
    logger.propagate = True
    log_dir = ROOT / ".tools"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "audio_fetcher.log"
    if not any(
        isinstance(handler, logging.FileHandler)
        and Path(handler.baseFilename) == log_path.resolve()
        for handler in logger.handlers
    ):
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _sanitize(name: str) -> str:
    """Remove filesystem-unsafe characters, keep Cyrillic."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:120] if len(name) > 120 else name


class AudioFetcher:
    def __init__(self, binary_path: Path | None = None) -> None:
        self.binary_path = binary_path or AUDIODL_PATH
        self.log = _logger()

    def fetch(self, url: str, output_path: str | Path) -> bool:
        """Low-level fetch: tools/audiodl.exe -x --audio-format opus --audio-quality 0 -o "{output_path}.%(ext)s" "{url}" """
        if not self.binary_path.is_file():
            msg = "audiodl.exe не найден в tools/"
            self.log.error("binary missing path=%s exists=%s url=%s", self.binary_path, self.binary_path.is_file(), url)
            self.log.info("binary check path=%s full_path=%s command=%s", self.binary_path, Path(__file__).parent.parent / "tools" / "audiodl.exe", [str(self.binary_path), "-x", "--audio-format", "opus"])
            return False

        output_path = Path(output_path)
        # output_path is without extension; yt-dlp will add .%(ext)s
        output_template = f"{output_path}.%(ext)s"

        # check if final .opus already exists — caller should handle exists, but double-check
        if (output_path.with_suffix(".opus")).is_file():
            self.log.info("File already exists, skipping download: %s", output_path.with_suffix(".opus"))
            return True

        command = [
            str(self.binary_path),
            "-x",
            "--audio-format",
            "opus",
            "--audio-quality",
            "0",
            "-o",
            output_template,
            url,
        ]
        self.log.info("Fetching %s -> %s", url, output_template)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.error("Audio fetch failed for %s: %s", url, exc)
            return False

        if result.returncode != 0:
            self.log.error(
                "Audio downloader exited code=%s path=%s command=%s url=%s stderr=%s stdout=%s",
                result.returncode,
                self.binary_path,
                command,
                url,
                result.stderr.strip()[:1200],
                result.stdout.strip()[:1200],
            )
            return False

        # Find downloaded file (could be .opus, .m4a, etc but we forced opus)
        downloaded = output_path.with_suffix(".opus")
        if not downloaded.is_file():
            # try to find any file with output_path basename
            candidates = list(output_path.parent.glob(output_path.name + ".*"))
            downloaded = candidates[0] if candidates else None
            if not downloaded or not downloaded.is_file():
                self.log.error("Download reported success but file not found: %s", output_template)
                return False

        # Size limit 50 MB
        try:
            size = downloaded.stat().st_size
            if size > MAX_FILE_SIZE:
                self.log.warning("File too large (%d bytes > 50MB), removing: %s", size, downloaded)
                downloaded.unlink(missing_ok=True)
                return False
        except OSError as exc:
            self.log.error("Could not check file size for %s: %s", downloaded, exc)
            return False

        self.log.info("Audio fetch completed for %s to %s (%d bytes)", url, downloaded, size if 'size' in locals() else 0)
        return True

    def fetch_with_tags(
        self,
        url: str,
        output_path: Path,
        artist: str,
        title: str,
        album: str = "",
        year: str = "",
        track_no: str = "",
        preview_duration: float | None = None,
    ) -> dict[str, Any]:
        """Fetch and then tag file + cover. Returns status dict. If preview_duration<40 use YouTube."""
        # BUG1.3: if Deezer preview <40 sec, auto use YouTube search
        if preview_duration is not None and preview_duration < 40 and url and "deezer" in url.lower():
            self.log.info("Deezer preview %.1f sec <40, switching to YouTube for %s - %s", preview_duration, artist, title)
            yt_url = self.search_youtube(artist, title)
            if yt_url:
                url = yt_url
                self.log.info("Switched to YouTube URL %s for %s - %s", url, artist, title)
        output_path = Path(output_path)
        final_path = output_path.with_suffix(".opus")
        if final_path.is_file():
            self.log.info("File already exists: %s", final_path)
            return {"status": "exists", "path": str(final_path)}
        self.log.info("fetch_with_tags start url=%s artist=%s title=%s output=%s", url, artist, title, output_path)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Задача 3: полный путь Path(__file__).parent.parent / "tools" / "audiodl.exe"
        expected = Path(__file__).parent.parent / "tools" / "audiodl.exe"
        self.log.info("check binary path=%s expected=%s exists=%s", self.binary_path, expected, self.binary_path.is_file())
        if not self.binary_path.is_file():
            msg = "audiodl.exe не найден в tools/"
            self.log.error("binary missing path=%s command=%s url=%s", self.binary_path, [str(self.binary_path), "-x", "--audio-format", "opus"], url)
            return {"status": "error", "message": msg, "detail": msg}
        ok = self.fetch(url, output_path)
        if not ok:
            # BUG2: include full yt-dlp log, already logged
            return {"status": "error", "detail": "Download failed or too large. Проверьте .tools/audio_fetcher.log"}

        # Determine downloaded file (should be .opus)
        downloaded = final_path
        if not downloaded.is_file():
            # fallback: find
            candidates = list(output_path.parent.glob(output_path.name + ".*"))
            downloaded = candidates[0] if candidates else None
            if not downloaded:
                return {"status": "error", "detail": "File not found after download"}

        # Size check already done in fetch, double-check
        try:
            if downloaded.stat().st_size > MAX_FILE_SIZE:
                downloaded.unlink(missing_ok=True)
                return {"status": "error", "detail": "File exceeds 50 MB"}
        except OSError:
            pass

        # Tagging via mutagen
        try:
            self._tag_file(downloaded, artist, title, album, year, track_no)
        except Exception as exc:
            self.log.warning("Tagging failed for %s: %s", downloaded, exc)

        # Cover from Deezer -> iTunes
        try:
            cover_url = self._fetch_cover_url(artist, title)
            if cover_url:
                self._embed_cover(downloaded, cover_url)
        except Exception as exc:
            self.log.warning("Cover handling failed for %s: %s", downloaded, exc)

        return {"status": "ok", "path": str(downloaded)}

    def _tag_file(self, path: Path, artist: str, title: str, album: str, year: str, track_no: str) -> None:
        try:
            from mutagen.oggopus import OggOpus
            from mutagen import File

            # Try OggOpus first
            if path.suffix.lower() == ".opus":
                try:
                    audio = OggOpus(str(path))
                except Exception:
                    audio = File(str(path), easy=True)
            else:
                audio = File(str(path), easy=True)

            if audio is None:
                self.log.warning("Mutagen could not open file for tagging: %s", path)
                return

            # For OggOpus, tags are Vorbis comments
            if path.suffix.lower() == ".opus" and hasattr(audio, "tags") and audio.tags is not None:
                audio["artist"] = artist
                audio["title"] = title
                if album:
                    audio["album"] = album
                if year:
                    audio["date"] = year
                if track_no:
                    audio["tracknumber"] = str(track_no)
                audio.save()
            elif audio is not None:
                # easy mode
                try:
                    audio["artist"] = artist
                    audio["title"] = title
                    if album:
                        audio["album"] = album
                    if year:
                        audio["date"] = year
                    if track_no:
                        audio["tracknumber"] = str(track_no)
                    audio.save()
                except Exception:
                    # fallback: set via tags dict
                    if hasattr(audio, "tags") and audio.tags is not None:
                        audio.tags["artist"] = artist
                        audio.tags["title"] = title
                        audio.save()
            self.log.info("Tagged %s: %s — %s", path, artist, title)
        except ImportError:
            self.log.warning("mutagen not available, skipping tagging for %s", path)
        except Exception as exc:
            self.log.warning("Tagging error for %s: %s", path, exc)

    def _fetch_cover_url(self, artist: str, title: str) -> str | None:
        # Try Deezer
        try:
            with httpx.Client(timeout=6.0) as client:
                resp = client.get(
                    "https://api.deezer.com/search",
                    params={"q": f"{artist} {title}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("data") or []
                    for item in results:
                        album = item.get("album") or {}
                        cover = album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium")
                        if cover and cover.startswith("https://"):
                            self.log.info("Deezer cover found for %s — %s: %s", artist, title, cover)
                            return cover
        except Exception as exc:
            self.log.warning("Deezer cover fetch failed for %s — %s: %s", artist, title, exc)

        # Fallback iTunes
        try:
            with httpx.Client(timeout=6.0) as client:
                resp = client.get(
                    "https://itunes.apple.com/search",
                    params={"term": f"{artist} {title}", "entity": "song", "limit": 1},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("results") or []
                    for item in results:
                        cover = item.get("artworkUrl100")
                        if cover:
                            # upgrade to 600x600
                            cover = cover.replace("100x100", "600x600")
                            if cover.startswith("https://"):
                                self.log.info("iTunes cover found for %s — %s: %s", artist, title, cover)
                                return cover
        except Exception as exc:
            self.log.warning("iTunes cover fetch failed for %s — %s: %s", artist, title, exc)

        return None

    def _embed_cover(self, audio_path: Path, cover_url: str) -> None:
        try:
            with httpx.Client(timeout=8.0) as client:
                resp = client.get(cover_url, timeout=8.0)
                resp.raise_for_status()
                image_data = resp.content
                # Validate size
                if len(image_data) > 5 * 1024 * 1024:
                    self.log.warning("Cover too large, skipping embed: %s", cover_url)
                    return

                # Try to embed via mutagen
                from mutagen.oggopus import OggOpus
                from mutagen.flac import Picture
                from PIL import Image
                import io
                import base64

                # Convert to jpeg if needed and resize
                try:
                    img = Image.open(io.BytesIO(image_data))
                    if img.mode in ("RGBA", "LA"):
                        bg = Image.new("RGB", img.size, (0, 0, 0))
                        bg.paste(img, mask=img.split()[-1])
                        img = bg
                    img = img.convert("RGB")
                    # resize to 600 max
                    if max(img.size) > 600:
                        img.thumbnail((600, 600))
                    out = io.BytesIO()
                    img.save(out, format="JPEG", quality=85)
                    image_data = out.getvalue()
                    mime = "image/jpeg"
                except Exception:
                    mime = "image/jpeg" if cover_url.lower().endswith(".jpg") or cover_url.lower().endswith(".jpeg") else "image/png"

                try:
                    audio = OggOpus(str(audio_path))
                    picture = Picture()
                    picture.data = image_data
                    picture.type = 3  # front cover
                    picture.mime = mime
                    picture.width = 600
                    picture.height = 600
                    picture.depth = 24
                    # Vorbis picture block
                    audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
                    audio.save()
                    self.log.info("Embedded cover for %s from %s", audio_path, cover_url)
                except Exception as exc:
                    # fallback: save cover.jpg next to file
                    cover_path = audio_path.with_suffix(".jpg")
                    cover_path.write_bytes(image_data)
                    self.log.info("Saved cover as sidecar %s for %s (embed failed: %s)", cover_path, audio_path, exc)
        except Exception as exc:
            self.log.warning("Cover embed failed for %s: %s", audio_path, exc)

    def search_youtube(self, artist: str, title: str) -> str | None:
        """Bug1: search full track on YouTube via ytsearch: \"{artist} - {title}\" official audio"""
        if not self.binary_path.is_file():
            self.log.error("Бинарник не найден. Положите audiodl.exe в папку tools/")
            return None
        # spec query exact: "{artist} - {title}" official audio
        query = f'"{artist} - {title}" official audio'
        # fallback without quotes if empty
        if not artist.strip() or not title.strip():
            query = f"{artist} - {title} official audio"
        url = f"ytsearch1:{query}"
        command = [str(self.binary_path), "--dump-json", "--no-playlist", url]
        # log
        self.log.info("search_youtube %s -> %s", f"{artist} - {title}", url)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                self.log.error("search_youtube failed %s: %s %s", query, result.stderr[:800], result.stdout[:800])
                return None
            for line in result.stdout.splitlines():
                line=line.strip()
                if not line:
                    continue
                try:
                    data=json.loads(line)
                    candidate=data.get("webpage_url") or data.get("url") or data.get("original_url")
                    if candidate and candidate.startswith("http"):
                        self.log.info("search_youtube found %s - %s: %s", artist, title, candidate)
                        return candidate
                except json.JSONDecodeError:
                    continue
        except Exception as exc:
            self.log.error("search_youtube error %s: %s", query, exc)
        return None

    def search_url(self, artist: str, title: str) -> str | None:
        """Search YouTube URL via audiodl.exe --dump-json (compat wrapper for search_youtube)"""
        # try search_youtube first for full audio
        url = self.search_youtube(artist, title)
        if url:
            return url
        if not self.binary_path.is_file():
            self.log.error("Downloader missing for search")
            return None
        query = f"{artist} - {title}"
        # Use yt-dlp search: ytsearch1:query
        url = f"ytsearch1:{query}"
        command = [str(self.binary_path), "--dump-json", "--no-playlist", url]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                self.log.warning("Search failed for %s: %s", query, result.stderr[:300])
                return None
            # yt-dlp outputs one JSON per line
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    # Prefer url or webpage_url
                    candidate = data.get("webpage_url") or data.get("url") or data.get("original_url")
                    if candidate and candidate.startswith("http"):
                        self.log.info("Search found for %s — %s: %s", artist, title, candidate)
                        return candidate
                except json.JSONDecodeError:
                    continue
        except Exception as exc:
            self.log.error("Search error for %s: %s", query, exc)
        return None

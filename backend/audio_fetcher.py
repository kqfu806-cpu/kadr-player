"""Optional adapter for a locally supplied audio downloader executable."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import ROOT

AUDIODL_PATH = ROOT / "tools" / "audiodl.exe"


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


class AudioFetcher:
    def __init__(self, binary_path: Path | None = None) -> None:
        self.binary_path = binary_path or AUDIODL_PATH
        self.log = _logger()

    def fetch(self, url: str, output_path: str | Path) -> bool:
        if not self.binary_path.is_file():
            self.log.error("Audio downloader executable is missing: %s", self.binary_path)
            return False

        command = [
            str(self.binary_path),
            "-x",
            "--audio-format",
            "opus",
            "-o",
            str(output_path),
            url,
        ]
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
                "Audio downloader exited with code %s for %s: %s",
                result.returncode,
                url,
                result.stderr.strip(),
            )
            return False

        self.log.info("Audio fetch completed for %s to %s", url, output_path)
        return True

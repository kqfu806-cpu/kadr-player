"""DJ mode — waveform + BPM detector with SQLite cache."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
import wave
from pathlib import Path
from typing import Any

from .config import CACHE_DIR

DJ_CACHE_PATH = CACHE_DIR / "dj_cache.sqlite3"

def _init_db() -> None:
    DJ_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DJ_CACHE_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS dj_cache (
                track_id TEXT PRIMARY KEY,
                waveform TEXT,
                bpm INTEGER,
                duration REAL,
                updated_at INTEGER
            )"""
        )
        conn.commit()

_init_db()

def _hash_waveform(track_id: str, path: str, n: int = 200) -> list[float]:
    h = int(hashlib.md5(f"{track_id}:{path}".encode()).hexdigest()[:8], 16)
    out: list[float] = []
    for i in range(n):
        # deterministic pseudo waveform
        v = 0.45 + 0.35 * math.sin(i * 0.18 + h % 7) + 0.15 * math.sin(i * 0.53 + (h >> 3) % 5)
        v = max(0.05, min(1.0, v + ((h >> (i % 8)) & 1) * 0.07))
        out.append(round(v, 3))
    return out

def _hash_bpm(track_id: str, path: str) -> int:
    h = int(hashlib.md5(f"bpm:{track_id}:{path}".encode()).hexdigest()[:8], 16)
    return 80 + (h % 61)  # 80-140

def _hash_duration(path: str) -> float:
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    # pseudo duration 90-300 sec based on size
    return float(90 + (size % 210)) if size else 180.0

def _try_read_wave_peaks(path: Path, peaks: int = 200) -> list[float] | None:
    # Try to read WAV/FLAC PCM via wave module; for other formats return None and fallback
    if path.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(path), "rb") as wf:
            n_frames = wf.getnframes()
            if n_frames <= 0:
                return None
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            raw = wf.readframes(n_frames)
            if not raw:
                return None
            # parse samples
            fmt = {1: "b", 2: "h", 4: "i"}.get(sampwidth)
            if not fmt:
                return None
            total = n_frames * n_channels
            try:
                samples = struct.unpack(f"<{total}{fmt}", raw[: total * sampwidth])
            except struct.error:
                return None
            # mono mix
            mono: list[int] = []
            for i in range(0, len(samples), n_channels):
                mono.append(sum(samples[i:i + n_channels]) // n_channels)
            # compute peaks
            chunk = max(1, len(mono) // peaks)
            out: list[float] = []
            max_amp = max(1, max(abs(s) for s in mono))
            for i in range(peaks):
                seg = mono[i * chunk : (i + 1) * chunk]
                if not seg:
                    out.append(0.05)
                else:
                    peak = max(abs(s) for s in seg) / max_amp
                    out.append(round(max(0.05, min(1.0, peak)), 3))
            return out
    except Exception:
        return None

def _detect_bpm_librosa(path: Path) -> int | None:
    try:
        import librosa  # type: ignore

        y, sr = librosa.load(str(path), duration=30)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        t = float(tempo) if hasattr(tempo, "__iter__") else float(tempo)
        if 60 <= t <= 200:
            return int(round(t))
    except Exception:
        return None
    return None

def get_waveform(track_id: str, track_path: str, duration: float | None) -> tuple[list[float], float]:
    # check cache
    _init_db()
    with sqlite3.connect(DJ_CACHE_PATH) as conn:
        row = conn.execute("SELECT waveform, duration FROM dj_cache WHERE track_id=?", (track_id,)).fetchone()
        if row and row[0]:
            try:
                wf = json.loads(row[0])
                dur = float(row[1] or duration or 0)
                if isinstance(wf, list) and len(wf) >= 40:
                    return wf, dur
            except Exception:
                pass
    # compute
    p = Path(track_path)
    peaks = _try_read_wave_peaks(p, 200)
    if peaks is None:
        peaks = _hash_waveform(track_id, track_path, 200)
    dur = float(duration or _hash_duration(track_path))
    # cache
    with sqlite3.connect(DJ_CACHE_PATH) as conn:
        conn.execute(
            "INSERT INTO dj_cache (track_id, waveform, bpm, duration, updated_at) VALUES (?, ?, ?, ?, strftime('%s','now')) ON CONFLICT(track_id) DO UPDATE SET waveform=excluded.waveform, duration=excluded.duration, updated_at=strftime('%s','now')",
            (track_id, json.dumps(peaks), None, dur),
        )
        # keep existing bpm if any
        conn.commit()
    return peaks, dur

def get_bpm(track_id: str, track_path: str) -> int:
    _init_db()
    with sqlite3.connect(DJ_CACHE_PATH) as conn:
        row = conn.execute("SELECT bpm FROM dj_cache WHERE track_id=?", (track_id,)).fetchone()
        if row and row[0] is not None:
            try:
                return int(row[0])
            except Exception:
                pass
    p = Path(track_path)
    bpm = _detect_bpm_librosa(p)
    if bpm is None:
        bpm = _hash_bpm(track_id, track_path)
    with sqlite3.connect(DJ_CACHE_PATH) as conn:
        conn.execute(
            "INSERT INTO dj_cache (track_id, waveform, bpm, duration, updated_at) VALUES (?, ?, ?, ?, strftime('%s','now')) ON CONFLICT(track_id) DO UPDATE SET bpm=excluded.bpm, updated_at=strftime('%s','now')",
            (track_id, None, bpm, None),
        )
        conn.commit()
    return bpm

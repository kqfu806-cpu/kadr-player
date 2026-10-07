"""SQLite-backed listening statistics."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import STATS_DB_PATH

PERIODS = {"day", "week", "month", "all"}


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS plays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            track_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            duration REAL NOT NULL CHECK (duration > 30),
            artist TEXT NOT NULL,
            title TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_plays_timestamp ON plays(timestamp)"
    )
    connection.commit()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _period_start(period: str, now: datetime | None = None) -> str | None:
    if period not in PERIODS:
        raise ValueError(f"Unsupported period: {period}")
    current = now or datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc)
    if period == "day":
        start = datetime.combine(current.date(), time.min, tzinfo=timezone.utc)
    elif period == "week":
        start = current - timedelta(days=7)
    elif period == "month":
        start = current - timedelta(days=30)
    else:
        return None
    return start.isoformat()


def record_play(
    track_id: str,
    session_id: str,
    duration: float,
    artist: str,
    title: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Insert or update a play after the listener crossed the 30-second threshold."""
    if not track_id.strip() or not artist.strip() or not title.strip():
        raise ValueError("track_id, artist, and title are required")
    if not session_id.strip():
        raise ValueError("session_id is required")
    if duration <= 30:
        raise ValueError("duration must exceed 30 seconds")
    db_path = db_path or STATS_DB_PATH
    timestamp = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO plays (session_id, track_id, timestamp, duration, artist, title)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                duration = MAX(plays.duration, excluded.duration)
            WHERE plays.track_id = excluded.track_id
            """,
            (session_id, track_id, timestamp, duration, artist.strip(), title.strip()),
        )
        row = connection.execute(
            "SELECT id, track_id, timestamp, duration, artist, title FROM plays WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if row is None or row["track_id"] != track_id:
        raise ValueError("session_id is already associated with a different track")
    return dict(row)


def summary(db_path: Path | None = None) -> dict[str, Any]:
    db_path = db_path or STATS_DB_PATH
    now = datetime.now(timezone.utc)
    result: dict[str, Any] = {}
    with _connect(db_path) as connection:
        for period in ("day", "week", "month", "all"):
            start = _period_start(period, now)
            if start is None:
                row = connection.execute(
                    "SELECT COUNT(*) AS plays, COALESCE(SUM(duration), 0) AS seconds FROM plays"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS plays, COALESCE(SUM(duration), 0) AS seconds "
                    "FROM plays WHERE timestamp >= ?",
                    (start,),
                ).fetchone()
            result[period] = {
                "tracks": int(row["plays"]),
                "minutes": round(float(row["seconds"]) / 60, 1),
            }
    return result


def top(
    period: str = "all", limit: int = 10, db_path: Path | None = None
) -> dict[str, Any]:
    db_path = db_path or STATS_DB_PATH
    start = _period_start(period)
    limit = max(1, min(int(limit), 10))
    with _connect(db_path) as connection:
        if start is None:
            where, params = "", ()
        else:
            where, params = "WHERE timestamp >= ?", (start,)
        tracks = connection.execute(
            f"""
            SELECT artist, title, COUNT(*) AS plays, SUM(duration) AS seconds
            FROM plays {where}
            GROUP BY artist COLLATE NOCASE, title COLLATE NOCASE
            ORDER BY plays DESC, seconds DESC, artist COLLATE NOCASE
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        artists = connection.execute(
            f"""
            SELECT artist, COUNT(*) AS plays, SUM(duration) AS seconds
            FROM plays {where}
            GROUP BY artist COLLATE NOCASE
            ORDER BY plays DESC, seconds DESC, artist COLLATE NOCASE
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return {
        "period": period,
        "artists": [_top_row(row) for row in artists],
        "tracks": [_top_row(row) for row in tracks],
    }


def _top_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "artist": row["artist"],
        "title": row["title"] if "title" in row.keys() else None,
        "plays": int(row["plays"]),
        "minutes": round(float(row["seconds"]) / 60, 1),
    }


def timeline(days: int = 30, db_path: Path | None = None) -> dict[str, Any]:
    days = max(1, min(int(days), 365))
    db_path = db_path or STATS_DB_PATH
    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days - 1)
    start = datetime.combine(start_date, time.min, tzinfo=timezone.utc).isoformat()
    with _connect(db_path) as connection:
        daily = connection.execute(
            """
            SELECT substr(timestamp, 1, 10) AS bucket,
                   COUNT(*) AS plays, COALESCE(SUM(duration), 0) AS seconds
            FROM plays WHERE timestamp >= ?
            GROUP BY bucket ORDER BY bucket
            """,
            (start,),
        ).fetchall()
        hourly = connection.execute(
            """
            SELECT substr(timestamp, 12, 2) AS bucket,
                   COUNT(*) AS plays, COALESCE(SUM(duration), 0) AS seconds
            FROM plays WHERE timestamp >= ?
            GROUP BY bucket ORDER BY bucket
            """,
            (start,),
        ).fetchall()

    daily_map = {row["bucket"]: row for row in daily}
    hourly_map = {int(row["bucket"]): row for row in hourly}
    return {
        "days": [
            {
                "date": (start_date + timedelta(days=index)).isoformat(),
                "plays": int(daily_map[(start_date + timedelta(days=index)).isoformat()]["plays"])
                if (start_date + timedelta(days=index)).isoformat() in daily_map
                else 0,
                "minutes": round(
                    float(daily_map[(start_date + timedelta(days=index)).isoformat()]["seconds"])
                    / 60,
                    1,
                )
                if (start_date + timedelta(days=index)).isoformat() in daily_map
                else 0,
            }
            for index in range(days)
        ],
        "hours": [
            {
                "hour": hour,
                "plays": int(hourly_map[hour]["plays"]) if hour in hourly_map else 0,
                "minutes": round(float(hourly_map[hour]["seconds"]) / 60, 1)
                if hour in hourly_map
                else 0,
            }
            for hour in range(24)
        ],
        "timezone": "UTC",
    }


def genre_artists(db_path: Path | None = None, limit: int = 10) -> list[dict[str, Any]]:
    db_path = db_path or STATS_DB_PATH
    with _connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT artist, SUM(duration) AS seconds
            FROM plays
            WHERE timestamp >= ?
            GROUP BY artist COLLATE NOCASE
            ORDER BY seconds DESC
            LIMIT ?
            """,
            (
                (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(),
                max(1, min(limit, 10)),
            ),
        ).fetchall()
    return [{"artist": row["artist"], "seconds": float(row["seconds"])} for row in rows]


def csv_rows(db_path: Path | None = None) -> list[dict[str, Any]]:
    db_path = db_path or STATS_DB_PATH
    with _connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT track_id, timestamp, duration, artist, title
            FROM plays ORDER BY timestamp DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]

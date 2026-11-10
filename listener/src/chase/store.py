"""SQLite bark log: the listener's single source of truth (see ADR 0003).

Every write is idempotent (primary keys + INSERT OR IGNORE), so replaying the same data after a
restart never duplicates anything.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chase.model import BarkWindow, Episode, Violation

_SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    start_ts REAL PRIMARY KEY,
    end_ts REAL NOT NULL,
    confidence REAL NOT NULL,
    loudness REAL NOT NULL,
    burst_start INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS episodes_start ON episodes(start_ts);
CREATE TABLE IF NOT EXISTS violations (
    id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    window_end_ts REAL NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY (id, revision)
);
"""


def _ts(d: datetime) -> float:
    return d.timestamp()


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


def _window_to_dict(w: BarkWindow) -> dict[str, Any]:
    return {
        "start": _ts(w.start),
        "end": _ts(w.end),
        "confidence": w.confidence,
        "loudness_dbfs": w.loudness_dbfs,
    }


def _window_from_dict(d: dict[str, Any]) -> BarkWindow:
    return BarkWindow(_dt(d["start"]), _dt(d["end"]), d["confidence"], d["loudness_dbfs"])


def _episode_to_json(e: Episode) -> str:
    return json.dumps(
        {
            "id": e.id,
            "start": _ts(e.start),
            "end": _ts(e.end),
            "windows": [_window_to_dict(w) for w in e.windows],
            "bark_count": e.bark_count,
            "barking_seconds": e.barking_seconds,
            "longest_gap_seconds": e.longest_gap_seconds,
            "mean_loudness_dbfs": e.mean_loudness_dbfs,
            "merged_from": e.merged_from,
        }
    )


def _episode_from_json(s: str) -> Episode:
    d = json.loads(s)
    return Episode(
        id=d["id"],
        start=_dt(d["start"]),
        end=_dt(d["end"]),
        windows=tuple(_window_from_dict(w) for w in d["windows"]),
        bark_count=d["bark_count"],
        barking_seconds=d["barking_seconds"],
        longest_gap_seconds=d["longest_gap_seconds"],
        mean_loudness_dbfs=d["mean_loudness_dbfs"],
        merged_from=d["merged_from"],
    )


def _violation_to_json(v: Violation) -> str:
    return json.dumps(
        {
            "id": v.id,
            "window_start": _ts(v.window_start),
            "window_end": _ts(v.window_end),
            "episode_ids": list(v.episode_ids),
            "revision": v.revision,
        }
    )


def _violation_from_json(s: str) -> Violation:
    d = json.loads(s)
    return Violation(
        id=d["id"],
        window_start=_dt(d["window_start"]),
        window_end=_dt(d["window_end"]),
        episode_ids=tuple(d["episode_ids"]),
        revision=d["revision"],
    )


class BarkLog:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path
        if read_only:
            self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        self._db.close()

    def add_window(self, w: BarkWindow, burst_start: bool) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO windows VALUES (?, ?, ?, ?, ?)",
            (_ts(w.start), _ts(w.end), w.confidence, w.loudness_dbfs, int(burst_start)),
        )
        self._db.commit()

    def windows_between(self, start: datetime, end: datetime) -> list[BarkWindow]:
        rows = self._db.execute(
            "SELECT start_ts, end_ts, confidence, loudness FROM windows "
            "WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts",
            (_ts(start), _ts(end)),
        )
        return [BarkWindow(_dt(s), _dt(e), c, lo) for s, e, c, lo in rows]

    def last_window_end(self) -> datetime | None:
        (ts,) = self._db.execute("SELECT MAX(end_ts) FROM windows").fetchone()
        return None if ts is None else _dt(ts)

    def burst_starts_between(self, start: datetime, end: datetime) -> list[datetime]:
        rows = self._db.execute(
            "SELECT start_ts FROM windows WHERE burst_start = 1 "
            "AND start_ts >= ? AND start_ts < ? ORDER BY start_ts",
            (_ts(start), _ts(end)),
        )
        return [_dt(ts) for (ts,) in rows]

    def save_episode(self, e: Episode) -> bool:
        cur = self._db.execute(
            "INSERT OR IGNORE INTO episodes VALUES (?, ?, ?, ?)",
            (e.id, _ts(e.start), _ts(e.end), _episode_to_json(e)),
        )
        self._db.commit()
        return cur.rowcount == 1

    def episodes_between(self, start: datetime, end: datetime) -> list[Episode]:
        rows = self._db.execute(
            "SELECT json FROM episodes WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts",
            (_ts(start), _ts(end)),
        )
        return [_episode_from_json(s) for (s,) in rows]

    def last_episode_end(self) -> datetime | None:
        (ts,) = self._db.execute("SELECT MAX(end_ts) FROM episodes").fetchone()
        return None if ts is None else _dt(ts)

    def save_violation(self, v: Violation) -> bool:
        cur = self._db.execute(
            "INSERT OR IGNORE INTO violations VALUES (?, ?, ?, ?)",
            (v.id, v.revision, _ts(v.window_end), _violation_to_json(v)),
        )
        self._db.commit()
        return cur.rowcount == 1

    def latest_violations(self, since: datetime) -> list[Violation]:
        rows = self._db.execute(
            "SELECT v.json FROM violations v JOIN ("
            "  SELECT id, MAX(revision) AS rev FROM violations GROUP BY id"
            ") m ON v.id = m.id AND v.revision = m.rev "
            "WHERE v.window_end_ts >= ? ORDER BY v.id",
            (_ts(since),),
        )
        return [_violation_from_json(s) for (s,) in rows]

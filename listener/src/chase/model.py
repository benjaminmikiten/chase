"""Core value types. All datetimes are timezone-aware UTC."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class BarkWindow:
    start: datetime
    end: datetime
    confidence: float
    loudness_dbfs: float


@dataclass(frozen=True)
class Episode:
    id: str
    start: datetime
    end: datetime
    windows: tuple[BarkWindow, ...]
    bark_count: int
    barking_seconds: float
    longest_gap_seconds: float
    mean_loudness_dbfs: float
    merged_from: int

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class RunStatus:
    start: datetime
    duration_seconds: float
    seconds_until_episode: float


@dataclass(frozen=True)
class Violation:
    id: str
    window_start: datetime
    window_end: datetime
    episode_ids: tuple[str, ...]
    revision: int

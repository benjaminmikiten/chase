"""A summary of the barking in a stretch of time, for reports (`GET /api/day`).

Read-only over the bark log: bursts (the same count as `chase_barks_total`), barking time, the
episodes and violations the rules already recorded, and how the stretch compares with the 30 days
before it. Nothing here re-runs the rules, so the report always agrees with the evidence.

The JSON is a contract with other apps (fireball prints it at 10pm), so it carries a
`schema_version`: bump it on any incompatible change.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from chase.model import BarkWindow, Episode, Violation
from chase.rules import barking_seconds

SCHEMA_VERSION = 1
MAX_SPAN = timedelta(days=7)
BASELINE_DAYS = 30


class Log(Protocol):
    def burst_starts_between(self, start: datetime, end: datetime) -> list[datetime]: ...
    def windows_between(self, start: datetime, end: datetime) -> list[BarkWindow]: ...
    def episodes_between(self, start: datetime, end: datetime) -> list[Episode]: ...
    def latest_violations(self, since: datetime) -> list[Violation]: ...


def _iso(d: datetime) -> str:
    return d.isoformat().replace("+00:00", "Z")


def summarize(log: Log, start: datetime, end: datetime, tz: ZoneInfo) -> dict[str, Any]:
    """Barking from `start` (inclusive) to `end` (exclusive). Raises ValueError on a bad span."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end need a timezone")
    if not timedelta(0) < end - start <= MAX_SPAN:
        raise ValueError(f"the span must be positive and at most {MAX_SPAN.days} days")

    bursts = log.burst_starts_between(start, end)
    windows = log.windows_between(start, end)
    by_hour: Counter[datetime] = Counter(
        b.astimezone(tz).replace(minute=0, second=0, microsecond=0) for b in bursts
    )
    hours = []
    h = start.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    while h < end:
        hours.append({"start": _iso(h), "barks": by_hour.get(h, 0)})
        h = (h.astimezone(ZoneInfo("UTC")) + timedelta(hours=1)).astimezone(tz)  # DST-safe

    span_days = (end - start) / timedelta(days=1)
    prior = log.burst_starts_between(start - timedelta(days=BASELINE_DAYS), start)

    episodes = log.episodes_between(start, end)
    violations = [
        v for v in log.latest_violations(start) if v.window_start < end and v.window_end >= start
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "start": _iso(start),
        "end": _iso(end),
        "timezone": tz.key,
        "barks": len(bursts),
        "barking_seconds": round(barking_seconds(windows), 1) if windows else 0.0,
        "typical_barks": round(len(prior) / BASELINE_DAYS * span_days, 1),
        "baseline_days": BASELINE_DAYS,
        "hours": hours,
        "episodes": [
            {
                "id": e.id,
                "start": _iso(e.start),
                "end": _iso(e.end),
                "duration_seconds": round(e.duration_seconds, 1),
                "barking_seconds": round(e.barking_seconds, 1),
                "bark_count": e.bark_count,
                "mean_loudness_dbfs": round(e.mean_loudness_dbfs, 1),
            }
            for e in episodes
        ],
        "violations": [
            {
                "id": v.id,
                "window_start": _iso(v.window_start),
                "window_end": _iso(v.window_end),
                "episodes": len(v.episode_ids),
                "revision": v.revision,
            }
            for v in violations
        ],
    }

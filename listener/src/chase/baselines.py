"""30-day barking baselines, computed locally so the dashboard doesn't depend on Grafana's
retention (Grafana Cloud's free tier keeps ~14 days). Pure functions over burst start times."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Baselines:
    barks_this_hour: int
    typical_this_hour: float
    barks_today: int
    daily_average: float
    heatmap: dict[tuple[int, int], float]  # (weekday 0=Mon, hour) -> average barks


def compute_baselines(
    burst_starts: Sequence[datetime], now: datetime, tz: ZoneInfo, days: int = 30
) -> Baselines:
    now_local = now.astimezone(tz)
    today = now_local.date()
    prior: set[date] = {today - timedelta(days=d) for d in range(1, days + 1)}

    this_hour = today_count = prior_total = 0
    prior_by_hour: Counter[int] = Counter()
    prior_by_cell: Counter[tuple[int, int]] = Counter()
    for b in burst_starts:
        lt = b.astimezone(tz)
        d = lt.date()
        if d == today:
            today_count += 1
            if lt.hour == now_local.hour:
                this_hour += 1
        elif d in prior:
            prior_total += 1
            prior_by_hour[lt.hour] += 1
            prior_by_cell[(lt.weekday(), lt.hour)] += 1

    weekday_counts = Counter(d.weekday() for d in prior)
    heatmap = {
        (wd, h): (prior_by_cell[(wd, h)] / weekday_counts[wd]) if weekday_counts[wd] else 0.0
        for wd in range(7)
        for h in range(24)
    }
    return Baselines(
        barks_this_hour=this_hour,
        typical_this_hour=prior_by_hour[now_local.hour] / days,
        barks_today=today_count,
        daily_average=prior_total / days,
        heatmap=heatmap,
    )


def hourly_history(
    burst_starts: Sequence[datetime], now: datetime, tz: ZoneInfo, days: int = 30
) -> list[dict]:
    """Barks per hour for the last `days*24` absolute hours, ending with the current hour.

    Hours are absolute (UTC-aligned), so DST can't add or drop a bucket; `tz` is accepted for
    symmetry with compute_baselines and future local-hour rendering by the report.
    """
    del tz
    current = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    first = current - timedelta(hours=days * 24 - 1)
    counts: Counter[datetime] = Counter(
        b.astimezone(UTC).replace(minute=0, second=0, microsecond=0) for b in burst_starts
    )
    return [
        {
            "hour_start": (first + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "barks": counts[first + timedelta(hours=i)],
        }
        for i in range(days * 24)
    ]

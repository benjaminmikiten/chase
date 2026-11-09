"""The rule engine. PURE: no I/O, no clock reads, no global state.

Everything here is a function of the bark log plus a RuleConfig, which is what makes the rules
tunable (`chase replay`) and restart-safe (recompute from the log at any time).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from statistics import fmean

from chase.config import RuleConfig
from chase.model import BarkWindow, Episode, RunStatus

_ID_FORMAT = "%Y%m%dT%H%M%SZ"


def episode_id(start: datetime) -> str:
    return "ep-" + start.strftime(_ID_FORMAT)


def _normalize(windows: Sequence[BarkWindow]) -> list[BarkWindow]:
    """Sort by start and drop exact duplicates, so input order never matters."""
    return sorted(set(windows), key=lambda w: (w.start, w.end))


def _gaps(windows: Sequence[BarkWindow]) -> list[float]:
    """Gap before each window after the first (sorted input); overlap counts as 0."""
    gaps: list[float] = []
    if not windows:
        return gaps
    reach = windows[0].end
    for w in windows[1:]:
        gaps.append(max(0.0, (w.start - reach).total_seconds()))
        reach = max(reach, w.end)
    return gaps


def _runs(windows: list[BarkWindow], max_gap_seconds: float) -> list[list[BarkWindow]]:
    runs: list[list[BarkWindow]] = []
    if not windows:
        return runs
    current = [windows[0]]
    for w, gap in zip(windows[1:], _gaps(windows), strict=True):
        if gap <= max_gap_seconds:
            current.append(w)
        else:
            runs.append(current)
            current = [w]
    runs.append(current)
    return runs


def _end(windows: Sequence[BarkWindow]) -> datetime:
    return max(w.end for w in windows)


def count_bursts(windows: Sequence[BarkWindow], hop_seconds: float) -> int:
    """Estimated bark count: maximal sequences of windows separated by at most one hop."""
    ws = _normalize(windows)
    if not ws:
        return 0
    return 1 + sum(1 for gap in _gaps(ws) if gap > hop_seconds)


def _barking_seconds(windows: list[BarkWindow]) -> float:
    total = 0.0
    seg_start, seg_end = windows[0].start, windows[0].end
    for w in windows[1:]:
        if w.start <= seg_end:
            seg_end = max(seg_end, w.end)
        else:
            total += (seg_end - seg_start).total_seconds()
            seg_start, seg_end = w.start, w.end
    return total + (seg_end - seg_start).total_seconds()


def _make_episode(windows: list[BarkWindow], merged_from: int, hop_seconds: float) -> Episode:
    gaps = _gaps(windows)
    return Episode(
        id=episode_id(windows[0].start),
        start=windows[0].start,
        end=_end(windows),
        windows=tuple(windows),
        bark_count=count_bursts(windows, hop_seconds),
        barking_seconds=_barking_seconds(windows),
        longest_gap_seconds=max(gaps, default=0.0),
        mean_loudness_dbfs=fmean(w.loudness_dbfs for w in windows),
        merged_from=merged_from,
    )


def find_episodes(
    windows: Sequence[BarkWindow], rules: RuleConfig, hop_seconds: float
) -> list[Episode]:
    ws = _normalize(windows)
    min_len = timedelta(minutes=rules.episode_min_minutes)
    qualifying = [r for r in _runs(ws, rules.max_gap_seconds) if _end(r) - r[0].start >= min_len]

    # Merge qualifying runs closer together than min_separation_minutes. A merged episode spans
    # both runs and includes every window between them.
    separation = timedelta(minutes=rules.min_separation_minutes)
    groups: list[list[list[BarkWindow]]] = []
    for run in qualifying:
        if groups and run[0].start - _end(groups[-1][-1]) < separation:
            groups[-1].append(run)
        else:
            groups.append([run])

    episodes: list[Episode] = []
    for group in groups:
        start, end = group[0][0].start, _end(group[-1])
        span = [w for w in ws if start <= w.start and w.end <= end]
        episodes.append(_make_episode(span, len(group), hop_seconds))
    return episodes


def finalized(episodes: Sequence[Episode], now: datetime, rules: RuleConfig) -> list[Episode]:
    """Episodes that can no longer grow or merge with later barking."""
    settle = timedelta(seconds=max(rules.max_gap_seconds, rules.min_separation_minutes * 60))
    return [e for e in episodes if now - e.end >= settle]


def current_run(
    windows: Sequence[BarkWindow], now: datetime, rules: RuleConfig
) -> RunStatus | None:
    ws = _normalize(windows)
    if not ws:
        return None
    last = _runs(ws, rules.max_gap_seconds)[-1]
    end = _end(last)
    if (now - end).total_seconds() > rules.max_gap_seconds:
        return None
    duration = (end - last[0].start).total_seconds()
    return RunStatus(
        start=last[0].start,
        duration_seconds=duration,
        seconds_until_episode=max(0.0, rules.episode_min_minutes * 60 - duration),
    )

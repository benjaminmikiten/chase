"""The listener loop: audio windows -> bark log -> rules -> evidence, metrics, events.

All decisions are recomputed from the SQLite bark log on every tick (ADR 0003). Episode and
violation IDs are deterministic and every write is idempotent, so a restart at any point loses
nothing and duplicates nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import numpy as np

from chase.audio import Capture, Spool, Windower, spool_keep_after
from chase.baselines import compute_baselines, hourly_history
from chase.config import Settings
from chase.detector import loudness_dbfs, to_16k
from chase.events import emit
from chase.evidence import iso, prune_backlog, write_backlog_clip, write_episode, write_violation
from chase.metrics import Metrics
from chase.model import BarkWindow, Episode, RunStatus, Violation
from chase.rules import assign_violations, current_run, finalized, find_episodes
from chase.store import BarkLog

logger = logging.getLogger(__name__)

TICK_SECONDS = 10.0
BASELINE_EVERY = timedelta(minutes=10)
MAX_TIMEOUTS_BEFORE_RESTART = 30
_VIOLATION_LOOKBACK = timedelta(hours=48)


class Scorer(Protocol):
    def score(self, audio_16k: np.ndarray) -> float: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


class Service:
    def __init__(
        self,
        settings: Settings,
        log: BarkLog,
        detector: Scorer,
        spool: Spool,
        metrics: Metrics,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.s = settings
        self.rules = settings.rules
        self.log = log
        self.detector = detector
        self.spool = spool
        self.metrics = metrics
        self.clock = clock
        self.tz = ZoneInfo(settings.timezone)
        self.outbox = settings.data_dir / "outbox"
        self.backlog = settings.data_dir / "backlog"
        self.model_name = settings.model_path.name
        self._last_bark_end = log.last_window_end()
        self._last_baseline: datetime | None = None
        metrics.episodes_per_window.set(self.rules.episodes_per_window)

    # -- per window ---------------------------------------------------------------------------

    def on_window(
        self, start: datetime, end: datetime, audio_16k: np.ndarray, audio_native: np.ndarray
    ) -> None:
        self.metrics.heartbeat.set(self.clock().timestamp())
        try:
            score = self.detector.score(audio_16k)
        except Exception:
            logger.exception("inference failed")
            self.metrics.audio_errors.labels(reason="inference").inc()
            return
        if score < self.rules.confidence_threshold:
            return
        loudness = loudness_dbfs(audio_native)
        gate = self.rules.min_loudness_dbfs
        if gate is not None and loudness < gate:
            return

        burst_start = (
            self._last_bark_end is None
            or (start - self._last_bark_end).total_seconds() > self.s.hop_seconds
        )
        self.log.add_window(BarkWindow(start, end, score, loudness), burst_start)
        self._last_bark_end = max(end, self._last_bark_end or end)
        self.metrics.barking_windows.inc()
        if burst_start:
            self.metrics.barks.inc()
        self.metrics.confidence.observe(score)
        self.metrics.loudness.observe(loudness)
        try:
            write_backlog_clip(
                self.backlog, start, audio_native, self.s.capture_rate, score, loudness
            )
        except Exception:
            logger.exception("backlog clip failed")
            self.metrics.audio_errors.labels(reason="evidence").inc()

    def on_capture_timeout(self) -> None:
        # The heartbeat only advances in on_window, so a dead mic stops it (alerting watches it).
        self.metrics.audio_errors.labels(reason="capture").inc()
        emit("capture_error", reason="no audio within timeout")

    # -- periodic -----------------------------------------------------------------------------

    def tick(self) -> None:
        now = self.clock()
        window = timedelta(hours=self.rules.violation_window_hours)
        # Fixed lookback (not "since the last saved episode"): an episode whose write failed is
        # retried on every tick, across restarts, until it ages out. IDs are deterministic, so
        # re-finding saved episodes is a no-op.
        cutoff = now - window - timedelta(hours=2)
        windows = self.log.windows_between(cutoff, now + timedelta(seconds=1))

        episodes = self._complete_episodes(
            find_episodes(windows, self.rules, self.s.hop_seconds), cutoff, now
        )
        run = current_run(windows, now, self.rules)
        final = finalized(episodes, now, self.rules, run.start if run else None)
        new_episodes = self._finalize_episodes(final)
        violations = self._update_violations(now)
        self._announce(new_episodes, violations)
        self._update_gauges(now, run, violations)
        final_ids = {e.id for e in final}
        pending_start = min((e.start for e in episodes if e.id not in final_ids), default=None)
        self._prune(now, run, pending_start)
        if self._last_baseline is None or now - self._last_baseline >= BASELINE_EVERY:
            self._refresh_baselines(now)

    def _complete_episodes(
        self, episodes: list[Episode], cutoff: datetime, now: datetime
    ) -> list[Episode]:
        """Drop episodes the lookback may have cut short.

        - One starting right at the cutoff may be the tail of a longer run.
        - One overlapping an already-saved episode with a different ID is part of that
          (earlier, longer or merged) episode.
        """
        margin = timedelta(seconds=self.rules.max_gap_seconds + self.s.window_seconds)
        saved = [
            e for e in self.log.episodes_between(cutoff - timedelta(days=2), now) if e.end >= cutoff
        ]
        out = []
        for e in episodes:
            if e.start < cutoff + margin:
                continue
            if any(s.id != e.id and s.start <= e.end and e.start <= s.end for s in saved):
                continue
            out.append(e)
        return out

    def _finalize_episodes(self, final: list[Episode]) -> list[Episode]:
        saved: list[Episode] = []
        for e in final:
            clip_start = e.start - timedelta(seconds=self.s.pre_roll_seconds)
            clip_end = e.end + timedelta(seconds=self.s.post_roll_seconds)

            def render(path, a=clip_start, b=clip_end):
                return self.spool.write_clip(path, a, b)

            try:
                # Evidence first, then the log: a crash in between re-runs as a no-op write.
                write_episode(
                    self.outbox,
                    e,
                    render,
                    self.s.capture_rate,
                    clip_start,
                    self.s.device_label,
                    self.rules,
                    self.model_name,
                )
            except Exception:
                logger.exception("writing episode %s failed", e.id)
                self.metrics.audio_errors.labels(reason="evidence").inc()
                continue
            if self.log.save_episode(e):
                self.metrics.episodes.inc()
                saved.append(e)
        return saved

    def _update_violations(self, now: datetime) -> list[Violation]:
        since = now - _VIOLATION_LOOKBACK
        existing = self.log.latest_violations(since)
        recent = self.log.episodes_between(since, now + timedelta(seconds=1))
        updated = assign_violations(existing, recent, self.rules)
        known = {(v.id, v.revision) for v in existing}
        for v in updated:
            if (v.id, v.revision) in known:
                continue
            bursts = self.log.burst_starts_between(
                now - timedelta(days=30, hours=1), now + timedelta(seconds=1)
            )
            try:
                write_violation(self.outbox, v, self.rules, hourly_history(bursts, now, self.tz))
            except Exception:
                logger.exception("writing violation %s failed", v.id)
                self.metrics.audio_errors.labels(reason="evidence").inc()
                continue
            if self.log.save_violation(v):
                if v.revision == 1:
                    self.metrics.violations.inc()
                emit(
                    "violation",
                    violation_id=v.id,
                    revision=v.revision,
                    episode_ids=list(v.episode_ids),
                )
        return updated

    def _announce(self, episodes: list[Episode], violations: list[Violation]) -> None:
        owner = {eid: v.id for v in violations for eid in v.episode_ids}
        for e in episodes:
            emit(
                "episode",
                episode_id=e.id,
                start=iso(e.start),
                end=iso(e.end),
                duration_seconds=round(e.duration_seconds, 1),
                barking_bursts_estimated=e.bark_count,
                violation_id=owner.get(e.id),
            )

    def _update_gauges(
        self, now: datetime, run: RunStatus | None, violations: list[Violation]
    ) -> None:
        m = self.metrics
        m.barking_now.set(1 if run else 0)
        m.current_run.set(run.duration_seconds if run else 0)
        m.until_episode.set(run.seconds_until_episode if run else 0)
        m.in_episode.set(1 if run and run.seconds_until_episode == 0 else 0)

        active = next((v for v in violations if v.window_start <= now < v.window_end), None)
        if active is not None:
            m.episodes_in_window.set(len(active.episode_ids))
        else:
            window = timedelta(hours=self.rules.violation_window_hours)
            assigned = {eid for v in violations for eid in v.episode_ids}
            recent = self.log.episodes_between(now - window, now + timedelta(seconds=1))
            m.episodes_in_window.set(sum(1 for e in recent if e.id not in assigned))

        pending = 0
        if (self.outbox / "episodes").exists():
            pending += sum(1 for p in (self.outbox / "episodes").iterdir() if p.is_dir())
        if (self.outbox / "violations").exists():
            pending += sum(1 for _ in (self.outbox / "violations").glob("*/rev-*.json"))
        m.outbox_pending.set(pending)

    def _prune(self, now: datetime, run: RunStatus | None, pending_start: datetime | None) -> None:
        # Protect the open run and any episode that has ended but isn't finalized yet.
        protect = [t for t in (run.start if run else None, pending_start) if t is not None]
        keep = spool_keep_after(
            now, min(protect, default=None), self.s.spool_minutes, self.s.pre_roll_seconds
        )
        try:
            self.spool.prune(keep)
        except Exception:
            logger.exception("spool prune failed")
            self.metrics.audio_errors.labels(reason="spool").inc()

    def _refresh_baselines(self, now: datetime) -> None:
        bursts = self.log.burst_starts_between(now - timedelta(days=31), now + timedelta(seconds=1))
        self.metrics.apply_baselines(compute_baselines(bursts, now, self.tz))
        prune_backlog(self.backlog, now, self.s.backlog_days)
        self._last_baseline = now

    # -- blocking loop ------------------------------------------------------------------------

    def run(self, capture: Capture) -> None:  # pragma: no cover - needs real audio hardware
        rate = self.s.capture_rate
        capture.start()
        t0 = self.clock()
        windower = Windower(rate, self.s.window_seconds, self.s.hop_seconds, t0)
        written = 0
        timeouts = 0
        last_tick = self.clock()
        while True:
            block = capture.read()
            if block is None:
                timeouts += 1
                self.on_capture_timeout()
                if timeouts >= MAX_TIMEOUTS_BEFORE_RESTART:
                    logger.warning("restarting audio capture after %d timeouts", timeouts)
                    capture.stop()
                    capture.start()
                    t0, written, timeouts = self.clock(), 0, 0
                    windower = Windower(rate, self.s.window_seconds, self.s.hop_seconds, t0)
            else:
                timeouts = 0
                try:
                    self.spool.write(t0 + timedelta(seconds=written / rate), block)
                except Exception:
                    logger.exception("spool write failed")
                    self.metrics.audio_errors.labels(reason="spool").inc()
                written += block.size
                for start, end, native in windower.push(block):
                    self.on_window(start, end, to_16k(native, rate), native)
            if (self.clock() - last_tick).total_seconds() >= TICK_SECONDS:
                last_tick = self.clock()
                try:
                    self.tick()
                except Exception:
                    logger.exception("tick failed")

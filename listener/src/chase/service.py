"""The listener loop: audio windows -> bark log -> rules -> evidence, metrics, events.

All decisions are recomputed from the SQLite bark log on every tick (ADR 0003). Episode and
violation IDs are deterministic and every write is idempotent, so a restart at any point loses
nothing and duplicates nothing.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from statistics import fmean
from typing import Protocol
from zoneinfo import ZoneInfo

import numpy as np

from chase.audio import Capture, CaptureBlock, Spool, Windower, spool_keep_after
from chase.baselines import compute_baselines, hourly_history
from chase.config import Settings
from chase.detector import loudness_dbfs, to_16k
from chase.events import emit
from chase.evidence import iso, prune_backlog, write_backlog_clip, write_episode, write_violation
from chase.metrics import Metrics
from chase.model import BarkWindow, Episode, RunStatus, Violation
from chase.rules import assign_violations, bursts, current_run, finalized, find_episodes
from chase.store import BarkLog

logger = logging.getLogger(__name__)

TICK_SECONDS = 10.0
BASELINE_EVERY = timedelta(minutes=10)
MAX_TIMEOUTS_BEFORE_RESTART = 30
CLOCK_CORRECT_SECONDS = 0.25
CLOCK_JUMP_SECONDS = 2.0
BACKLOG_CLIP = timedelta(seconds=5)
BACKLOG_EVERY = timedelta(seconds=60)
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
        # Backlog clips are cut for bursts that end after this point (history is skipped).
        self._backlog_cursor = clock()
        self._t0: datetime | None = None
        self._written = 0
        self._windower: Windower | None = None
        metrics.episodes_per_window.set(self.rules.episodes_per_window)

    # -- per window ---------------------------------------------------------------------------

    def on_window(
        self, start: datetime, end: datetime, audio_16k: np.ndarray, audio_native: np.ndarray
    ) -> None:
        try:
            score = self.detector.score(audio_16k)
        except Exception:
            logger.exception("inference failed")
            self.metrics.audio_errors.labels(reason="inference").inc()
            return
        # Only after a successful score: a broken model must stop the heartbeat too.
        self.metrics.heartbeat.set(self.clock().timestamp())
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
        self._write_backlog(windows, now)
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

    def _write_backlog(self, windows: list[BarkWindow], now: datetime) -> None:
        """One short clip per finished burst (plus one per minute of a long burst), local only.

        Clips come from the spool, so they never duplicate overlapping windows' audio.
        """
        settle = timedelta(seconds=self.s.hop_seconds + 1.0)
        for burst in bursts(
            [w for w in windows if w.start >= self._backlog_cursor], self.s.hop_seconds
        ):
            end = max(w.end for w in burst)
            if now - end < settle:
                break  # still going; later bursts are newer still
            t = burst[0].start
            while t < end:
                clip_end = min(end, t + BACKLOG_CLIP)
                part = [w for w in burst if t <= w.start < clip_end] or burst
                try:
                    write_backlog_clip(
                        self.backlog,
                        t,
                        self.spool.extract(t, clip_end),
                        self.s.capture_rate,
                        max(w.confidence for w in part),
                        fmean(w.loudness_dbfs for w in part),
                    )
                except Exception:
                    logger.exception("backlog clip failed")
                    self.metrics.audio_errors.labels(reason="evidence").inc()
                t += BACKLOG_EVERY
            self._backlog_cursor = end

    def _refresh_baselines(self, now: datetime) -> None:
        bursts = self.log.burst_starts_between(now - timedelta(days=31), now + timedelta(seconds=1))
        self.metrics.apply_baselines(compute_baselines(bursts, now, self.tz))
        prune_backlog(self.backlog, now, self.s.backlog_days)
        try:
            self.metrics.data_free.set(shutil.disk_usage(self.s.data_dir).free)
        except OSError:
            logger.exception("disk usage check failed")
        self._last_baseline = now

    # -- blocking loop ------------------------------------------------------------------------

    def process_block(self, block: CaptureBlock) -> None:
        """Stamp a captured block, spool it, and score any windows it completes.

        Timestamps come from the sample count (sample-accurate within a stretch) but are
        re-anchored to the system clock whenever they drift: small drift (mic crystal error)
        shifts the anchor in place; a jump (dropped audio, USB hiccup, capture restart) starts
        a fresh anchor and window stream.
        """
        rate = self.s.capture_rate
        if block.overflow:
            self.metrics.audio_errors.labels(reason="overflow").inc()
        derived = self._t0 + timedelta(seconds=self._written / rate) if self._t0 else None
        drift = (block.start - derived).total_seconds() if derived else 0.0
        if self._windower is None or abs(drift) > CLOCK_JUMP_SECONDS:
            if self._windower is not None:
                self.metrics.clock_corrections.inc()
                emit("clock_reanchored", kind="jump", seconds=round(drift, 3))
            self._t0, self._written = block.start, 0
            self._windower = Windower(rate, self.s.window_seconds, self.s.hop_seconds, block.start)
        elif abs(drift) > CLOCK_CORRECT_SECONDS:
            self._t0 = self._t0 + timedelta(seconds=drift) if self._t0 else block.start
            self._windower.shift(drift)
            self.metrics.clock_corrections.inc()
            emit("clock_reanchored", kind="drift", seconds=round(drift, 3))

        assert self._t0 is not None
        try:
            self.spool.write(self._t0 + timedelta(seconds=self._written / rate), block.samples)
        except Exception:
            logger.exception("spool write failed")
            self.metrics.audio_errors.labels(reason="spool").inc()
        self._written += block.samples.size
        for start, end, native in self._windower.push(block.samples):
            self.on_window(start, end, to_16k(native, rate), native)

    def safe_tick(self) -> None:
        try:
            self.tick()
        except Exception:
            logger.exception("tick failed")
            self.metrics.audio_errors.labels(reason="tick").inc()
            return
        self.metrics.last_tick_success.set(self.clock().timestamp())

    def _start_capture(
        self, capture: Capture, stop: threading.Event, sleep: Callable[[float], None]
    ) -> bool:
        """Start the mic, retrying with backoff (1 s doubling to 30 s). False if stopped."""
        backoff = 1.0
        while not stop.is_set():
            try:
                capture.start()
                return True
            except Exception as e:
                logger.warning("audio capture failed to start: %s", e)
                self.metrics.audio_errors.labels(reason="capture").inc()
                emit("capture_error", reason=f"start failed: {e}")
                sleep(backoff)
                backoff = min(backoff * 2, 30.0)
        return False

    def _stop_capture(self, capture: Capture) -> None:
        try:
            capture.stop()
        except Exception:
            logger.exception("audio capture failed to stop cleanly")

    def run(
        self,
        capture: Capture,
        stop: threading.Event | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """The blocking loop. Returns when `stop` is set (SIGTERM), after flushing the spool."""
        stop = stop or threading.Event()
        if not self._start_capture(capture, stop, sleep):
            return
        timeouts = 0
        last_tick = self.clock()
        try:
            while not stop.is_set():
                block = capture.read()
                if block is None:
                    timeouts += 1
                    self.on_capture_timeout()
                    if timeouts >= MAX_TIMEOUTS_BEFORE_RESTART:
                        logger.warning("restarting audio capture after %d timeouts", timeouts)
                        self._stop_capture(capture)
                        self._windower = None  # next block starts a fresh anchor
                        timeouts = 0
                        if not self._start_capture(capture, stop, sleep):
                            break
                else:
                    timeouts = 0
                    self.process_block(block)
                if (self.clock() - last_tick).total_seconds() >= TICK_SECONDS:
                    last_tick = self.clock()
                    self.safe_tick()
        finally:
            self._stop_capture(capture)
            try:
                self.spool.flush()
            except Exception:
                logger.exception("spool flush on shutdown failed")

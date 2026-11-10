"""The live loop: timestamps, capture failures, shutdown. Uses a fake mic and a fake clock."""

import threading
from datetime import datetime, timedelta

import numpy as np
import pytest
from prometheus_client import CollectorRegistry

from chase.audio import CaptureBlock, Spool
from chase.config import load_settings
from chase.metrics import Metrics
from chase.service import Service
from chase.store import BarkLog
from tests.conftest import PRESETS, T0

RATE = 16000
BLOCK = 1600  # 0.1 s


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class LowScorer:
    def score(self, audio_16k):
        return 0.0


class FailingScorer:
    def score(self, audio_16k):
        raise RuntimeError("model broke")


class FakeCapture:
    """Yields scripted CaptureBlocks (None = timeout); stops the service when exhausted."""

    def __init__(self, items, clock: Clock, stop: threading.Event, start_failures: int = 0):
        self.items = list(items)
        self.clock = clock
        self.stop_event = stop
        self.start_failures = start_failures
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1
        if self.start_failures:
            self.start_failures -= 1
            raise RuntimeError("no such device")

    def stop(self):
        self.stops += 1

    def read(self, timeout: float = 2.0):
        if not self.items:
            self.stop_event.set()
            return None
        item = self.items.pop(0)
        if item is not None:
            self.clock.t = item.start + timedelta(seconds=item.samples.size / RATE)
        else:
            self.clock.t += timedelta(seconds=timeout)
        return item


@pytest.fixture
def svc(tmp_path):
    def _make(scorer=None):
        cfg = tmp_path / "chase.yaml"
        cfg.write_text(
            f"rules:\n  preset: san-antonio\ncapture_rate: {RATE}\ndata_dir: {tmp_path / 'd'}\n"
        )
        settings = load_settings(cfg, PRESETS)
        clock = Clock(T0)
        reg = CollectorRegistry()
        service = Service(
            settings,
            BarkLog(settings.data_dir / "chase.sqlite"),
            scorer or LowScorer(),
            Spool(settings.data_dir / "spool", RATE),
            Metrics(reg),
            clock,
        )
        return service, clock, reg

    return _make


def blocks(start: datetime, n: int, wall_step: float = 0.1):
    """n blocks of 0.1 s of audio whose wall-clock start advances by wall_step each."""
    sig = np.full(BLOCK, 0.01, dtype=np.float32)
    return [CaptureBlock(start + timedelta(seconds=i * wall_step), sig, False) for i in range(n)]


def record_windows(service: Service) -> list[datetime]:
    seen: list[datetime] = []
    real = service.on_window

    def spy(start, end, a16, native):
        seen.append(start)
        real(start, end, a16, native)

    service.on_window = spy  # type: ignore[method-assign]
    return seen


def test_timestamps_follow_wall_clock_despite_mic_drift(svc):
    service, clock, reg = svc()
    seen = record_windows(service)
    # The mic delivers 0.1 s of samples every 0.1001 s of wall time (1000 ppm slow clock).
    items = blocks(T0, 10000, wall_step=0.1001)
    for b in items:
        clock.t = b.start
        service.process_block(b)
    last_wall = items[-1].start
    assert abs((seen[-1] - last_wall).total_seconds()) < 1.5  # window is ~1 s behind its block
    assert reg.get_sample_value("chase_clock_corrections_total") >= 3


def test_short_gap_reanchors_instead_of_shifting_everything(svc):
    service, clock, _ = svc()
    seen = record_windows(service)
    before = blocks(T0, 50)
    after = blocks(T0 + timedelta(seconds=25), 50)  # 20 s of nothing (USB hiccup)
    for b in before + after:
        service.process_block(b)
    later = [s for s in seen if s >= T0 + timedelta(seconds=20)]
    assert later, "windows after the gap must be stamped after the gap"
    assert later[0] >= T0 + timedelta(seconds=25)


def test_overflow_is_counted(svc):
    service, _, reg = svc()
    b = blocks(T0, 1)[0]
    service.process_block(CaptureBlock(b.start, b.samples, True))
    assert reg.get_sample_value("chase_audio_errors_total", {"reason": "overflow"}) == 1


def test_capture_start_failures_retry_with_backoff(svc):
    service, clock, reg = svc()
    stop = threading.Event()
    cap = FakeCapture(blocks(T0, 20), clock, stop, start_failures=2)
    sleeps: list[float] = []
    service.run(cap, stop=stop, sleep=sleeps.append)  # type: ignore[arg-type]
    assert cap.starts == 3
    assert sleeps == [1.0, 2.0]
    # 2 failed starts + the fake's final empty read (counted as a timeout)
    assert reg.get_sample_value("chase_audio_errors_total", {"reason": "capture"}) == 3


def test_repeated_timeouts_restart_capture(svc):
    service, clock, reg = svc()
    stop = threading.Event()
    cap = FakeCapture([None] * 31 + blocks(T0 + timedelta(minutes=2), 20), clock, stop)
    service.run(cap, stop=stop, sleep=lambda s: None)  # type: ignore[arg-type]
    assert cap.starts == 2 and cap.stops >= 1
    assert reg.get_sample_value("chase_audio_errors_total", {"reason": "capture"}) >= 31


def test_run_flushes_spool_on_stop(svc):
    service, clock, _ = svc()
    stop = threading.Event()
    service.run(FakeCapture(blocks(T0, 30), clock, stop), stop=stop, sleep=lambda s: None)  # type: ignore[arg-type]
    audio = service.spool.extract(T0, T0 + timedelta(seconds=3))
    assert np.abs(audio).max() > 0.009  # the open segment reached disk/was kept, not lost
    assert list((service.s.data_dir / "spool").glob("*.flac"))


def test_tick_errors_are_counted_and_success_is_timestamped(svc, monkeypatch):
    service, clock, reg = svc()
    monkeypatch.setattr(service, "tick", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    service.safe_tick()
    assert reg.get_sample_value("chase_audio_errors_total", {"reason": "tick"}) == 1
    assert reg.get_sample_value("chase_last_tick_success_timestamp_seconds") == 0
    monkeypatch.undo()
    service.safe_tick()
    assert reg.get_sample_value("chase_last_tick_success_timestamp_seconds") == T0.timestamp()


def test_heartbeat_not_advanced_when_inference_fails(svc):
    service, clock, reg = svc(FailingScorer())
    for b in blocks(T0, 20):
        service.process_block(b)
    assert reg.get_sample_value("chase_audio_errors_total", {"reason": "inference"}) > 0
    assert reg.get_sample_value("chase_heartbeat_timestamp_seconds") == 0

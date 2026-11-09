import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from prometheus_client import CollectorRegistry

from chase.audio import Spool
from chase.config import Settings, load_settings
from chase.metrics import Metrics
from chase.service import Service
from chase.store import BarkLog
from tests.conftest import PRESETS, T0

RATE = 16000
HOP = 0.48
WIN = 0.96


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class ScriptedDetector:
    """Scores 0.9 inside barking intervals, 0.1 outside."""

    def __init__(self, intervals: list[tuple[datetime, datetime]]):
        self.intervals = intervals
        self.current: datetime | None = None

    def score(self, audio_16k: np.ndarray) -> float:
        assert self.current is not None
        return 0.9 if any(a <= self.current < b for a, b in self.intervals) else 0.1


class Rig:
    def __init__(self, tmp: Path, settings: Settings, detector: ScriptedDetector, clock: Clock):
        self.settings, self.detector, self.clock = settings, detector, clock
        self.log = BarkLog(settings.data_dir / "chase.sqlite")
        self.spool = Spool(settings.data_dir / "spool", RATE)
        self.registry = CollectorRegistry()
        self.metrics = Metrics(self.registry)
        self.service = Service(settings, self.log, detector, self.spool, self.metrics, clock)

    def feed(self, start: datetime, seconds: float, loudness_amp: float = 0.1) -> None:
        """Feed windows every hop across [start, start+seconds], advancing the clock."""
        audio = (loudness_amp * np.ones(int(WIN * RATE))).astype(np.float32)
        t = 0.0
        while t + WIN <= seconds + 1e-9:
            s = start + timedelta(seconds=t)
            self.detector.current = s
            self.clock.t = s + timedelta(seconds=WIN)
            self.service.on_window(s, s + timedelta(seconds=WIN), audio, audio)
            t += HOP

    def tick_at(self, t: datetime) -> None:
        self.clock.t = t
        self.service.tick()

    def value(self, name: str, labels: dict | None = None) -> float | None:
        return self.registry.get_sample_value(name, labels or {})


@pytest.fixture
def make_rig(tmp_path):
    def _make(intervals, extra: str = "", clock: Clock | None = None) -> Rig:
        cfg = tmp_path / "chase.yaml"
        cfg.write_text(
            f"rules:\n  preset: san-antonio\n{extra}capture_rate: {RATE}\n"
            f"data_dir: {tmp_path / 'data'}\n"
        )
        settings = load_settings(cfg, PRESETS)
        return Rig(tmp_path, settings, ScriptedDetector(intervals), clock or Clock(T0))

    return _make


def outbox(rig: Rig) -> Path:
    return rig.settings.data_dir / "outbox"


def events(capsys, name: str) -> list[dict]:
    out = []
    for line in capsys.readouterr().out.splitlines():
        rec = json.loads(line)
        if rec["event"] == name:
            out.append(rec)
    return out


def test_episode_written_once_after_separation(make_rig, capsys):
    end = T0 + timedelta(minutes=11)
    rig = make_rig([(T0, end)])
    rig.feed(T0, 11 * 60)
    rig.tick_at(end + timedelta(minutes=9))
    assert not (outbox(rig) / "episodes").exists() or not any((outbox(rig) / "episodes").iterdir())
    rig.tick_at(end + timedelta(minutes=10, seconds=1))
    dirs = list((outbox(rig) / "episodes").iterdir())
    assert [d.name for d in dirs] == ["ep-20261108T120000Z"]
    assert rig.value("chase_episodes_total") == 1
    rig.tick_at(end + timedelta(minutes=11))
    assert rig.value("chase_episodes_total") == 1
    assert len(events(capsys, "episode")) == 1


def test_two_episodes_make_violation_manifest(make_rig, capsys):
    a = (T0, T0 + timedelta(minutes=11))
    b = (T0 + timedelta(hours=12), T0 + timedelta(hours=12, minutes=11))
    rig = make_rig([a, b])
    rig.feed(a[0], 11 * 60)
    rig.tick_at(a[1] + timedelta(minutes=11))
    rig.feed(b[0], 11 * 60)
    rig.tick_at(b[1] + timedelta(minutes=11))
    rev1 = outbox(rig) / "violations" / "v-20261108T120000Z" / "rev-1.json"
    m = json.loads(rev1.read_text())
    assert m["episode_ids"] == ["ep-20261108T120000Z", "ep-20261109T000000Z"]
    assert len(m["hourly_history"]) == 720
    assert rig.value("chase_violations_total") == 1
    assert len(events(capsys, "violation")) == 1


def test_restart_mid_episode(make_rig, capsys):
    end = T0 + timedelta(minutes=11)
    clock = Clock(T0)
    rig = make_rig([(T0, end)], clock=clock)
    rig.feed(T0, 6 * 60)
    rig.tick_at(T0 + timedelta(minutes=6))
    rig.log.close()
    rig2 = make_rig([(T0, end)], clock=clock)
    rig2.feed(T0 + timedelta(minutes=6), 5 * 60)
    rig2.tick_at(end + timedelta(minutes=11))
    rig2.tick_at(end + timedelta(minutes=12))
    dirs = list((outbox(rig2) / "episodes").iterdir())
    assert [d.name for d in dirs] == ["ep-20261108T120000Z"]
    meta = json.loads((dirs[0] / "metadata.json").read_text())
    assert meta["end"].startswith("2026-11-08T12:10:5") or meta["end"].startswith(
        "2026-11-08T12:11"
    )
    assert len(events(capsys, "episode")) == 1


def test_restart_after_violation_no_new_revision(make_rig):
    a = (T0, T0 + timedelta(minutes=11))
    b = (T0 + timedelta(hours=2), T0 + timedelta(hours=2, minutes=11))
    clock = Clock(T0)
    rig = make_rig([a, b], clock=clock)
    rig.feed(a[0], 11 * 60)
    rig.feed(b[0], 11 * 60)
    rig.tick_at(b[1] + timedelta(minutes=11))
    rig.log.close()
    rig2 = make_rig([a, b], clock=clock)
    rig2.tick_at(b[1] + timedelta(minutes=12))
    files = sorted(p.name for p in (outbox(rig2) / "violations").rglob("*.json"))
    assert files == ["rev-1.json"]


def test_capture_timeout_counts_and_heartbeat_stalls(make_rig, capsys):
    rig = make_rig([])
    rig.feed(T0, 2.0)
    beat = rig.value("chase_heartbeat_timestamp_seconds")
    assert beat is not None
    for i in range(3):
        rig.clock.t = T0 + timedelta(seconds=10 + i)
        rig.service.on_capture_timeout()
    assert rig.value("chase_audio_errors_total", {"reason": "capture"}) == 3
    assert rig.value("chase_heartbeat_timestamp_seconds") == beat
    assert len(events(capsys, "capture_error")) == 3


def test_episode_clip_includes_pre_post_roll(make_rig):
    end = T0 + timedelta(minutes=11)
    rig = make_rig([(T0, end)])
    pre = T0 - timedelta(minutes=1)
    block = np.full(RATE, 0.05, dtype=np.float32)
    for s in range(13 * 60):
        rig.spool.write(pre + timedelta(seconds=s), block)
    rig.feed(T0, 11 * 60)
    rig.tick_at(end + timedelta(minutes=11))
    d = next((outbox(rig) / "episodes").iterdir())
    info = sf.info(str(d / "audio.flac"))
    meta = json.loads((d / "metadata.json").read_text())
    episode_len = (
        datetime.fromisoformat(meta["end"]) - datetime.fromisoformat(meta["start"])
    ).total_seconds()
    assert info.duration == pytest.approx(episode_len + 60, abs=WIN)
    data, _ = sf.read(str(d / "audio.flac"))
    assert np.abs(data).max() > 0.04  # real audio, not zero-fill


def test_loudness_gate(make_rig):
    rig = make_rig([(T0, T0 + timedelta(minutes=1))], extra="  min_loudness_dbfs: -30\n")
    rig.feed(T0, 5.0, loudness_amp=0.01)  # -40 dBFS
    assert rig.value("chase_barking_windows_total") == 0
    rig2 = make_rig([(T0, T0 + timedelta(minutes=1))])
    rig2.feed(T0 + timedelta(seconds=10), 5.0, loudness_amp=0.01)
    assert rig2.value("chase_barking_windows_total") > 0


def test_gauges_during_run(make_rig):
    rig = make_rig([(T0, T0 + timedelta(minutes=30))])
    rig.feed(T0, 7 * 60 + 42)
    rig.tick_at(rig.clock.t + timedelta(seconds=2))
    assert rig.value("chase_barking_now") == 1
    assert rig.value("chase_current_run_seconds") == pytest.approx(462, abs=1)
    assert rig.value("chase_seconds_until_episode") == pytest.approx(138, abs=1)
    assert rig.value("chase_in_episode") == 0
    assert rig.value("chase_episodes_per_window") == 2
    rig.tick_at(rig.clock.t + timedelta(minutes=1))
    assert rig.value("chase_barking_now") == 0


def test_prune_keeps_audio_for_long_unfinalized_episode(make_rig):
    # A 3-hour bout has ended but isn't finalized yet; spool_minutes is 120. The spool must still
    # hold its pre-roll so the clip is complete.
    end = T0 + timedelta(hours=3)
    rig = make_rig([(T0, end)], extra="spool_minutes: 120\n")
    first = T0 - timedelta(seconds=30)
    rig.spool.write(first, np.full(RATE * 60, 0.05, dtype=np.float32))
    rig.spool.flush()
    rig.feed(T0, 3 * 3600, loudness_amp=0.1)
    rig.tick_at(end + timedelta(minutes=5))  # run over, episode not yet final
    seg = rig.spool.extract(first, first + timedelta(seconds=30))
    assert np.abs(seg).max() > 0.04

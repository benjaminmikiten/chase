import hashlib
import json
import os
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path

import numpy as np
import soundfile as sf

from chase.evidence import (
    SCHEMA_VERSION,
    Gap,
    prune_backlog,
    write_backlog_clip,
    write_episode,
    write_violation,
)
from chase.model import Episode, Violation
from tests.conftest import T0, make_windows

RATE = 48000


def _episode() -> Episode:
    ws = tuple(make_windows(T0, 4.0))
    return Episode("ep-20261108T120000Z", T0, ws[-1].end, ws, 1, 4.0, 0.0, -20.0, 1)


def _audio(seconds: float) -> np.ndarray:
    return (0.1 * np.sin(np.arange(int(seconds * RATE)) / 10)).astype(np.float32)


def _render(seconds: float, gaps: Sequence[Gap] = ()) -> Callable[[Path], list[Gap]]:
    def render(path: Path) -> list[Gap]:
        sf.write(str(path), _audio(seconds), RATE, subtype="PCM_16", format="FLAC")
        return list(gaps)

    return render


def test_episode_files_and_hash(tmp_path, sa_rules):
    e = _episode()
    clip_start = e.start - timedelta(seconds=30)
    d = write_episode(tmp_path, e, _render(64), RATE, clip_start, "USB mic", sa_rules, "yamnet")
    assert d == tmp_path / "episodes" / e.id
    meta = json.loads((d / "metadata.json").read_text())
    flac = (d / "audio.flac").read_bytes()
    assert meta["schema_version"] == SCHEMA_VERSION == 1
    assert meta["episode_id"] == e.id
    assert meta["start"] == "2026-11-08T12:00:00Z"
    assert meta["clip_start"] == "2026-11-08T11:59:30Z"
    assert meta["audio"]["sha256"] == hashlib.sha256(flac).hexdigest()
    assert meta["audio"]["duration_seconds"] == 64
    assert meta["audio"]["format"] == "FLAC"
    assert meta["detector"] == {"model": "yamnet", "confidence_threshold": 0.7}
    assert meta["device"] == "USB mic"
    assert meta["barking_bursts_estimated"] == 1
    assert meta["audio"]["gaps"] == []
    assert meta["audio"]["complete"] is True
    assert len(meta["windows"]) == len(e.windows)
    assert not list((tmp_path / "episodes").glob("*.tmp"))


def test_episode_rewrite_is_noop(tmp_path, sa_rules):
    e = _episode()
    args = (tmp_path, e, _render(5), RATE, e.start, "USB mic", sa_rules, "yamnet")
    d = write_episode(*args)
    before = (d / "audio.flac").read_bytes()
    write_episode(*args)
    assert [p.name for p in (tmp_path / "episodes").iterdir()] == [e.id]
    assert (d / "audio.flac").read_bytes() == before


def test_violation_revision_files(tmp_path, sa_rules):
    hist = [{"hour_start": "2026-11-08T12:00:00Z", "barks": 3}]
    v1 = Violation("v-20261108T120000Z", T0, T0 + timedelta(hours=24), ("a", "b"), 1)
    v2 = Violation("v-20261108T120000Z", T0, T0 + timedelta(hours=24), ("a", "b", "c"), 2)
    p1 = write_violation(tmp_path, v1, sa_rules, hist)
    p2 = write_violation(tmp_path, v2, sa_rules, hist)
    assert p1 == tmp_path / "violations" / v1.id / "rev-1.json"
    assert p2.name == "rev-2.json" and p1.exists()
    m = json.loads(p2.read_text())
    assert m["episode_ids"] == ["a", "b", "c"]
    assert m["revision"] == 2
    assert m["window_end"] == "2026-11-09T12:00:00Z"
    assert m["rule"]["citation"] == sa_rules.citation
    assert m["rule"]["episodes_per_window"] == 2
    assert m["hourly_history"] == hist


def test_backlog_prune_90_days(tmp_path):
    now = T0
    old = write_backlog_clip(tmp_path, now - timedelta(days=91), _audio(1), RATE, 0.9, -20.0)
    new = write_backlog_clip(tmp_path, now - timedelta(days=89), _audio(1), RATE, 0.9, -20.0)
    assert old.exists() and old.with_suffix(".json").exists()
    assert old.parent.name == "2026-08-09"  # date folders, so pruning removes whole days
    assert prune_backlog(tmp_path, now, 90) == 1
    assert not old.parent.exists()
    assert new.exists()


def test_backlog_sidecar(tmp_path):
    p = write_backlog_clip(tmp_path, T0, _audio(1), RATE, 0.85, -22.5)
    meta = json.loads(p.with_suffix(".json").read_text())
    assert meta == {
        "start": "2026-11-08T12:00:00Z",
        "confidence": 0.85,
        "loudness_dbfs": -22.5,
        "sample_rate": RATE,
    }
    assert os.path.getsize(p) > 0


def test_episode_records_gaps(tmp_path, sa_rules):
    e = _episode()
    gap = (T0, T0 + timedelta(seconds=2))
    d = write_episode(tmp_path, e, _render(10, [gap]), RATE, e.start, "mic", sa_rules, "yamnet")
    meta = json.loads((d / "metadata.json").read_text())
    assert meta["audio"]["gaps"] == [
        {"start": "2026-11-08T12:00:00Z", "end": "2026-11-08T12:00:02Z", "seconds": 2.0}
    ]
    assert meta["audio"]["complete"] is False

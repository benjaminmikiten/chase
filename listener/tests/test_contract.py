"""The listener <-> report contract.

The report service's test fixtures are generated HERE, by the listener's real writer code, so the
two sides can't drift. Run with CHASE_UPDATE_FIXTURES=1 to regenerate; otherwise this test fails if
the committed fixtures no longer match what the listener writes.

All data is synthetic: invented times, no personal information.
"""

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from chase.baselines import hourly_history
from chase.evidence import write_episode, write_violation
from chase.model import BarkWindow, Episode, Violation
from chase.rules import count_bursts, episode_id, violation_id

FIXTURES = Path(__file__).resolve().parents[2] / "report" / "tests" / "fixtures"
RATE = 16000
TZ = "America/Chicago"


def _episode(start: datetime, minutes: float, gap_at: float | None = None) -> Episode:
    windows = []
    t = 0.0
    while t + 0.96 <= minutes * 60:
        s = start + timedelta(seconds=t)
        windows.append(BarkWindow(s, s + timedelta(seconds=0.96), 0.88, -24.5))
        t += 5.0
    ws = tuple(windows)
    gaps = [max(0.0, (b.start - a.end).total_seconds()) for a, b in zip(ws, ws[1:], strict=False)]
    return Episode(
        id=episode_id(start),
        start=start,
        end=ws[-1].end,
        windows=ws,
        bark_count=count_bursts(ws, 0.48),
        barking_seconds=len(ws) * 0.96,
        longest_gap_seconds=max(gaps),
        mean_loudness_dbfs=-24.5,
        merged_from=1,
    )


def _render(seconds: float, gaps):
    def render(path: Path):
        t = np.arange(int(seconds * RATE)) / RATE
        import soundfile as sf

        sf.write(
            str(path),
            (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32),
            RATE,
            subtype="PCM_16",
            format="FLAC",
        )
        return list(gaps)

    return render


def build_fixtures(root: Path, rules) -> None:
    """Write a bucket-shaped tree: episodes/<id>/{audio.flac,metadata.json}, violations/..."""
    a = _episode(datetime(2026, 11, 8, 12, 42, 10, tzinfo=UTC), 16.5)  # 06:42 CST
    b = _episode(datetime(2026, 11, 9, 4, 15, 2, tzinfo=UTC), 26.8)  # 22:15 CST Nov 8
    gap = (b.start + timedelta(minutes=3), b.start + timedelta(minutes=3, seconds=12))
    outbox = root / "bucket"
    for e, gaps in ((a, []), (b, [gap])):
        write_episode(
            outbox,
            e,
            _render(2.0, gaps),
            RATE,
            e.start - timedelta(seconds=30),
            "USB microphone (synthetic)",
            rules,
            "yamnet.tflite",
            timezone=TZ,
        )
    v = Violation(violation_id(a.start), a.start, a.start + timedelta(hours=24), (a.id, b.id), 1)
    bursts = [w.start for e in (a, b) for w in e.windows[::3]]
    write_violation(outbox, v, rules, hourly_history(bursts, b.end, None))  # type: ignore[arg-type]
    shutil.copy(outbox / "episodes" / a.id / "metadata.json", root / "episode_metadata.json")
    shutil.copy(outbox / "violations" / v.id / "rev-1.json", root / "violation_manifest.json")


def _json_tree(root: Path) -> dict[str, object]:
    out: dict[str, object] = {}
    for p in sorted(root.rglob("*.json")):
        d = json.loads(p.read_text())
        if "audio" in d:
            d["audio"].pop("sha256", None)  # FLAC bytes may differ across libsndfile builds
        out[str(p.relative_to(root))] = d
    return out


def test_write_contract_fixtures(tmp_path, sa_rules):
    if os.environ.get("CHASE_UPDATE_FIXTURES") == "1":
        shutil.rmtree(FIXTURES, ignore_errors=True)
        FIXTURES.mkdir(parents=True)
        build_fixtures(FIXTURES, sa_rules)
    build_fixtures(tmp_path, sa_rules)
    assert _json_tree(tmp_path) == _json_tree(FIXTURES), (
        "report fixtures are stale: rerun with CHASE_UPDATE_FIXTURES=1"
    )
    meta = json.loads((FIXTURES / "episode_metadata.json").read_text())
    assert meta["clock"] == {"source": "system clock (NTP-synced)", "timezone": TZ}

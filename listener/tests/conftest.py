from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chase.config import RuleConfig, load_preset
from chase.model import BarkWindow

REPO = Path(__file__).resolve().parents[2]
PRESETS = REPO / "config" / "presets"

T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
WINDOW = 0.96


def make_windows(
    start: datetime,
    seconds: float,
    hop: float = 0.48,
    gap_every: float | None = None,
    gap_len: float = 0.0,
    confidence: float = 0.9,
    loudness: float = -20.0,
) -> list[BarkWindow]:
    """Synthetic barking windows covering [start, start+seconds].

    If gap_every is set, a silent gap of gap_len seconds is inserted after every gap_every
    seconds of barking.
    """
    out: list[BarkWindow] = []
    t = 0.0
    since_gap = 0.0
    while t + WINDOW <= seconds + 1e-9:
        s = start + timedelta(seconds=t)
        out.append(BarkWindow(s, s + timedelta(seconds=WINDOW), confidence, loudness))
        t += hop
        since_gap += hop
        if gap_every is not None and since_gap >= gap_every:
            t += WINDOW + gap_len  # move past the current window, then stay silent
            since_gap = 0.0
    return out


@pytest.fixture
def sa_rules() -> RuleConfig:
    return load_preset(PRESETS / "san-antonio.yaml")

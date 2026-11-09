import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from chase.cli import main
from chase.rules import count_bursts
from chase.store import BarkLog
from tests.conftest import PRESETS, T0, make_windows


@pytest.fixture
def seeded(tmp_path) -> Path:
    data = tmp_path / "data"
    log = BarkLog(data / "chase.sqlite")
    for start in (T0, T0 + timedelta(hours=12)):
        ws = make_windows(start, 11 * 60)
        prev = None
        for w in ws:
            log.add_window(w, burst_start=prev is None or count_bursts([prev, w], 0.48) == 2)
            prev = w
    log.close()
    cfg = tmp_path / "chase.yaml"
    cfg.write_text(f"rules:\n  preset: san-antonio\ndata_dir: {data}\n")
    return cfg


def test_replay_prints_episodes_and_violations(seeded, capsys):
    rc = main(["replay", "--config", str(seeded), "--presets", str(PRESETS)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 episodes, 1 violations" in out
    assert "v-20261108T120000Z" in out
    assert "06:00" in out  # local time (12:00 UTC = 06:00 CST)
    assert not (seeded.parent / "data" / "outbox").exists()


def test_replay_alternate_preset_changes_result(seeded, tmp_path, capsys):
    strict = tmp_path / "strict.yaml"
    strict.write_text(
        (PRESETS / "san-antonio.yaml")
        .read_text()
        .replace("episode_min_minutes: 10", "episode_min_minutes: 15")
    )
    main(["replay", "--config", str(seeded), "--presets", str(PRESETS), "--preset", str(strict)])
    assert "0 episodes, 0 violations" in capsys.readouterr().out


def test_replay_since_until(seeded, capsys):
    main(
        [
            "replay",
            "--config",
            str(seeded),
            "--presets",
            str(PRESETS),
            "--since",
            "2026-11-08",
            "--until",
            "2026-11-08",
        ]
    )
    # Local Nov 8 covers 06:00 CST and 18:00 CST, so both bouts are included.
    assert "2 episodes" in capsys.readouterr().out


def test_replay_does_not_modify_db(seeded):
    db = seeded.parent / "data" / "chase.sqlite"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    main(["replay", "--config", str(seeded), "--presets", str(PRESETS)])
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_replay_missing_db_is_friendly(tmp_path, capsys):
    cfg = tmp_path / "chase.yaml"
    cfg.write_text(f"rules:\n  preset: san-antonio\ndata_dir: {tmp_path / 'none'}\n")
    assert main(["replay", "--config", str(cfg), "--presets", str(PRESETS)]) == 0
    assert "0 episodes, 0 violations" in capsys.readouterr().out

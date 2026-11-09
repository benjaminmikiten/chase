from pathlib import Path

import pytest

from chase.config import ConfigError, load_preset, load_settings

REPO = Path(__file__).resolve().parents[2]
PRESETS = REPO / "config" / "presets"


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "chase.yaml"
    p.write_text(text)
    return p


def test_loads_san_antonio_preset():
    rc = load_preset(PRESETS / "san-antonio.yaml")
    assert rc.name == "san-antonio"
    assert rc.confidence_threshold == 0.7
    assert rc.episode_min_minutes == 10
    assert rc.max_gap_seconds == 30
    assert rc.min_separation_minutes == 10
    assert rc.episodes_per_window == 2
    assert rc.violation_window_hours == 24
    assert "twenty-four (24) hours" in rc.citation
    assert "unverified" in rc.caveat
    assert rc.addressee == "San Antonio Animal Care Services / 311"
    assert rc.min_loudness_dbfs is None


def test_override_merges_over_preset(tmp_path):
    s = load_settings(
        write(tmp_path, "rules:\n  preset: san-antonio\n  max_gap_seconds: 45\n"), PRESETS
    )
    assert s.rules.max_gap_seconds == 45
    assert s.rules.episode_min_minutes == 10


def test_defaults_applied(tmp_path):
    s = load_settings(write(tmp_path, "rules:\n  preset: san-antonio\n"), PRESETS)
    assert s.timezone == "America/Chicago"
    assert s.hop_seconds == 0.48
    assert s.window_seconds == 0.96
    assert s.capture_rate == 48000
    assert s.pre_roll_seconds == 30
    assert s.post_roll_seconds == 30
    assert s.backlog_days == 90
    assert s.data_dir == Path("/data")
    assert s.metrics_port == 9100


def test_unknown_preset_raises(tmp_path):
    with pytest.raises(ConfigError, match="nowhere"):
        load_settings(write(tmp_path, "rules:\n  preset: nowhere\n"), PRESETS)


def test_unknown_key_raises(tmp_path):
    with pytest.raises(ConfigError, match="max_gap_second"):
        load_settings(
            write(tmp_path, "rules:\n  preset: san-antonio\n  max_gap_second: 45\n"), PRESETS
        )


def test_unknown_top_level_key_raises(tmp_path):
    with pytest.raises(ConfigError, match="hop_second"):
        load_settings(write(tmp_path, "rules:\n  preset: san-antonio\nhop_second: 1\n"), PRESETS)


@pytest.mark.parametrize(
    ("snippet", "key"),
    [
        ("rules:\n  preset: san-antonio\n  episodes_per_window: 0\n", "episodes_per_window"),
        ("rules:\n  preset: san-antonio\n  confidence_threshold: 1.5\n", "confidence_threshold"),
        ("rules:\n  preset: san-antonio\nhop_seconds: 0\n", "hop_seconds"),
        ("rules:\n  preset: san-antonio\ntimezone: Mars/Base\n", "timezone"),
        ("rules:\n  preset: san-antonio\ncapture_rate: 44100\n", "capture_rate"),
    ],
)
def test_invalid_values_raise(tmp_path, snippet, key):
    with pytest.raises(ConfigError, match=key):
        load_settings(write(tmp_path, snippet), PRESETS)

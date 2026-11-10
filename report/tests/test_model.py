import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chase_report.model import EpisodeMeta, Manifest

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def test_episode_from_fixture():
    e = EpisodeMeta.from_json(load("episode_metadata.json"))
    assert e.episode_id == "ep-20261108T124210Z"
    assert e.start == datetime(2026, 11, 8, 12, 42, 10, tzinfo=UTC)
    assert e.timezone == "America/Chicago"
    assert e.clock_source == "system clock (NTP-synced)"
    assert e.bursts >= 1
    assert e.complete is True and e.gaps == ()
    assert len(e.windows) > 100
    assert e.flac_sha256 and len(e.flac_sha256) == 64
    assert e.start.tzinfo is not None


def test_episode_with_gap():
    m = Manifest.from_json(load("violation_manifest.json"))
    second = m.episode_ids[1]
    e = EpisodeMeta.from_json(load(f"bucket/episodes/{second}/metadata.json"))
    assert e.complete is False
    assert len(e.gaps) == 1 and e.gaps[0].seconds == pytest.approx(12.0)


def test_manifest_from_fixture():
    m = Manifest.from_json(load("violation_manifest.json"))
    assert m.violation_id == "v-20261108T124210Z"
    assert m.revision == 1
    assert len(m.episode_ids) == 2
    assert "unverified" in m.rule.caveat
    assert m.rule.addressee.startswith("San Antonio")
    assert len(m.hourly_history) == 720
    assert sum(n for _, n in m.hourly_history) > 0


def test_unknown_schema_rejected():
    d = load("episode_metadata.json")
    d["schema_version"] = 99
    with pytest.raises(ValueError, match="schema_version"):
        EpisodeMeta.from_json(d)
    m = load("violation_manifest.json")
    m["schema_version"] = 2
    with pytest.raises(ValueError, match="schema_version"):
        Manifest.from_json(m)


def test_plan1_metadata_without_clock_defaults():
    d = load("episode_metadata.json")
    del d["clock"]
    e = EpisodeMeta.from_json(d)
    assert e.timezone == "UTC" and e.clock_source == "unknown"

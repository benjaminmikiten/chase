import json
import shutil
from pathlib import Path

import pytest

from chase_report.assemble import MissingEvidence, assemble, ffmpeg_mp3
from chase_report.model import Manifest
from tests.fakes import FakeStore, fake_encode

FIX = Path(__file__).parent / "fixtures"
BUCKET = FIX / "bucket"
V = "v-20261108T124210Z"


def manifest() -> Manifest:
    return Manifest.from_json(json.loads((FIX / "violation_manifest.json").read_text()))


def test_copies_and_mp3_per_episode(tmp_path):
    store = FakeStore.from_dir(BUCKET)
    ev = assemble(store, manifest(), tmp_path, encode=fake_encode)
    assert [e.meta.episode_id for e in ev.episodes] == list(manifest().episode_ids)
    for e in ev.episodes:
        base = f"evidence/{V}/episodes/{e.meta.episode_id}/"
        assert store.exists(base + "audio.flac") and store.exists(base + "metadata.json")
        assert e.mp3_name == base + "audio.mp3" and store.exists(e.mp3_name)
        assert len(e.mp3_sha256) == 64 and e.mp3_bytes > 0
        assert e.flac_name == base + "audio.flac"
    assert store.exists(f"evidence/{V}/rev-1/manifest.json")


def test_missing_episode_raises(tmp_path):
    store = FakeStore.from_dir(BUCKET)
    second = manifest().episode_ids[1]
    store.delete(f"episodes/{second}/metadata.json")
    with pytest.raises(MissingEvidence, match=second):
        assemble(store, manifest(), tmp_path, encode=fake_encode)


def test_one_episode_at_a_time(tmp_path):
    store = FakeStore.from_dir(BUCKET)
    peak = {"n": 0}

    def counting_encode(src: Path, dst: Path) -> None:
        fake_encode(src, dst)
        peak["n"] = max(peak["n"], len(list(tmp_path.iterdir())))

    assemble(store, manifest(), tmp_path, encode=counting_encode)
    assert peak["n"] <= 2  # one FLAC + its MP3, never more
    assert list(tmp_path.iterdir()) == []


@pytest.mark.ffmpeg
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_ffmpeg_mp3_real(tmp_path):
    src = BUCKET / "episodes" / manifest().episode_ids[0] / "audio.flac"
    dst = tmp_path / "a.mp3"
    ffmpeg_mp3(src, dst)
    assert dst.stat().st_size > 1000
    assert dst.read_bytes()[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3")


def test_ffmpeg_failure_raises(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    bad = tmp_path / "bad.flac"
    bad.write_bytes(b"not audio")
    with pytest.raises(RuntimeError, match="ffmpeg"):
        ffmpeg_mp3(bad, tmp_path / "x.mp3")

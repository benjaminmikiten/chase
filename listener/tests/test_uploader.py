import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry

from chase.config import load_settings
from chase.metrics import Metrics
from chase.uploader import Uploader, UploadError, build_uploader, prune_sent
from tests.conftest import PRESETS, REPO

V = "v-20261108T120000Z"


class FakeWriter:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.order: list[str] = []
        self.fail: set[str] = set()
        self.exists_already: set[str] = set()

    def create(self, name: str, path: Path, content_type: str) -> bool:
        if name in self.fail:
            raise UploadError(f"boom {name}")
        if name in self.exists_already or name in self.objects:
            return False
        self.objects[name] = path.read_bytes()
        self.order.append(name)
        return True


def episode(outbox: Path, eid: str) -> None:
    d = outbox / "episodes" / eid
    d.mkdir(parents=True)
    (d / "audio.flac").write_bytes(b"fLaC" + eid.encode())
    (d / "metadata.json").write_text(json.dumps({"episode_id": eid}))


def manifest(outbox: Path, ids: list[str], rev: int = 1) -> None:
    d = outbox / "violations" / V
    d.mkdir(parents=True, exist_ok=True)
    (d / f"rev-{rev}.json").write_text(json.dumps({"violation_id": V, "episode_ids": ids}))


@pytest.fixture
def rig(tmp_path):
    reg = CollectorRegistry()
    writer = FakeWriter()
    up = Uploader(tmp_path / "outbox", tmp_path / "sent", writer, Metrics(reg))
    return up, writer, reg, tmp_path


def test_episode_uploaded_then_moved(rig):
    up, w, _, tmp = rig
    episode(tmp / "outbox", "ep-1")
    assert up.run_once() == 1
    assert w.order == ["episodes/ep-1/audio.flac", "episodes/ep-1/metadata.json"]
    assert not (tmp / "outbox" / "episodes" / "ep-1").exists()
    assert (tmp / "sent" / "episodes" / "ep-1" / "audio.flac").exists()


def test_manifest_waits_for_its_episodes(rig):
    up, w, _, tmp = rig
    episode(tmp / "outbox", "ep-1")
    episode(tmp / "outbox", "ep-2")
    manifest(tmp / "outbox", ["ep-1", "ep-2"])
    w.fail = {"episodes/ep-2/audio.flac"}
    up.run_once()
    assert f"violations/{V}/rev-1.json" not in w.objects
    assert (tmp / "outbox" / "violations" / V / "rev-1.json").exists()
    w.fail = set()
    up.run_once()
    assert w.order[-1] == f"violations/{V}/rev-1.json"
    assert (tmp / "sent" / "violations" / V / "rev-1.json").exists()


def test_manifest_episode_uploaded_in_earlier_run(rig):
    up, w, _, tmp = rig
    episode(tmp / "outbox", "ep-1")
    up.run_once()
    manifest(tmp / "outbox", ["ep-1"])
    up.run_once()
    assert f"violations/{V}/rev-1.json" in w.objects


def test_412_counts_as_done(rig):
    up, w, _, tmp = rig
    episode(tmp / "outbox", "ep-1")
    w.exists_already = {"episodes/ep-1/audio.flac", "episodes/ep-1/metadata.json"}
    assert up.run_once() == 1
    assert (tmp / "sent" / "episodes" / "ep-1").exists()


def test_upload_error_counted_and_retried(rig):
    up, w, reg, tmp = rig
    episode(tmp / "outbox", "ep-1")
    episode(tmp / "outbox", "ep-2")
    w.fail = {"episodes/ep-1/metadata.json"}
    assert up.run_once() == 1  # ep-2 still goes through
    assert reg.get_sample_value("chase_audio_errors_total", {"reason": "upload"}) == 1
    assert (tmp / "outbox" / "episodes" / "ep-1").exists()
    w.fail = set()
    assert up.run_once() == 1


def test_tmp_dirs_ignored(rig):
    up, w, _, tmp = rig
    (tmp / "outbox" / "episodes" / "ep-1.tmp").mkdir(parents=True)
    (tmp / "outbox" / "violations" / V).mkdir(parents=True)
    (tmp / "outbox" / "violations" / V / "rev-1.json.tmp").write_text("{}")
    assert up.run_once() == 0 and w.order == []


def test_prune_sent(tmp_path):
    old = tmp_path / "sent" / "episodes" / "ep-old"
    new = tmp_path / "sent" / "episodes" / "ep-new"
    for d in (old, new):
        d.mkdir(parents=True)
        (d / "audio.flac").write_bytes(b"x")
    t = time.time() - 31 * 86400
    os.utime(old, (t, t))
    now = datetime.now(UTC)
    assert prune_sent(tmp_path / "sent", now, 30) == 1
    assert not old.exists() and new.exists()
    assert prune_sent(tmp_path / "missing", now + timedelta(days=1), 30) == 0


def _settings(tmp_path: Path, extra: str = ""):
    cfg = tmp_path / "chase.yaml"
    cfg.write_text(f"rules:\n  preset: san-antonio\ndata_dir: {tmp_path}\n{extra}")
    return load_settings(cfg, PRESETS)


def test_disabled_without_bucket(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    reg = CollectorRegistry()
    assert build_uploader(_settings(tmp_path), Metrics(reg)) is None
    assert "upload disabled" in caplog.text
    assert reg.get_sample_value("chase_audio_errors_total", {"reason": "upload"}) is None


def test_missing_key_is_counted_not_fatal(tmp_path):
    reg = CollectorRegistry()
    s = _settings(tmp_path, f"gcs_bucket: my-bucket\ngcs_credentials: {tmp_path / 'nope.json'}\n")
    assert build_uploader(s, Metrics(reg)) is None
    assert reg.get_sample_value("chase_audio_errors_total", {"reason": "upload"}) == 1


def test_existing_config_still_loads():
    s = load_settings(REPO / "config" / "chase.example.yaml", PRESETS)
    assert s.gcs_bucket is None
    assert s.gcs_credentials == Path("/secrets/listener-key.json")

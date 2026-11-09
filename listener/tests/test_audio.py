from datetime import timedelta

import numpy as np
import pytest

from chase import audio as audio_mod
from chase.audio import Capture, Spool, Windower, spool_keep_after
from tests.conftest import T0

RATE = 16000


def ramp(seconds: float, rate: int = RATE) -> np.ndarray:
    # Distinct, exactly representable-in-int16 samples so slices can be compared after FLAC.
    n = int(seconds * rate)
    return ((np.arange(n) % 30000) - 15000).astype(np.float32) / 32768.0


def test_windower_timestamps():
    w = Windower(RATE, 0.96, 0.48, T0)
    out = []
    block = int(0.1 * RATE)
    sig = ramp(3.0)
    for i in range(0, sig.size, block):
        out += w.push(sig[i : i + block])
    assert len(out) == 5
    starts = [s for s, _, _ in out]
    assert starts == [T0 + timedelta(seconds=0.48 * k) for k in range(5)]
    assert all(a.size == 15360 for _, _, a in out)
    assert out[0][1] - out[0][0] == timedelta(seconds=0.96)
    np.testing.assert_array_equal(out[1][2], sig[7680 : 7680 + 15360])


def test_spool_extract_across_segments(tmp_path):
    sp = Spool(tmp_path, RATE, segment_seconds=60)
    sig = ramp(150)
    block = RATE  # 1 s blocks
    for i in range(0, sig.size, block):
        sp.write(T0 + timedelta(seconds=i / RATE), sig[i : i + block])
    sp.flush()
    got = sp.extract(T0 + timedelta(seconds=50), T0 + timedelta(seconds=130))
    assert got.size == 80 * RATE
    np.testing.assert_allclose(got, sig[50 * RATE : 130 * RATE], atol=1 / 32768)


def test_spool_prune(tmp_path):
    sp = Spool(tmp_path, RATE, segment_seconds=60)
    sig = ramp(180)
    for i in range(0, sig.size, RATE):
        sp.write(T0 + timedelta(seconds=i / RATE), sig[i : i + RATE])
    sp.flush()
    assert len(list(tmp_path.glob("*.flac"))) == 3
    removed = sp.prune(T0 + timedelta(seconds=125))
    assert removed == 2
    assert len(list(tmp_path.glob("*.flac"))) == 1


def test_keep_after_protects_long_run():
    now = T0 + timedelta(minutes=100)
    assert spool_keep_after(now, T0, 60, 30) == T0 - timedelta(seconds=30)
    assert spool_keep_after(now, None, 60, 30) == now - timedelta(minutes=60)
    recent = now - timedelta(minutes=5)
    assert spool_keep_after(now, recent, 60, 30) == now - timedelta(minutes=60)


def test_extract_missing_span_zero_fills(tmp_path):
    sp = Spool(tmp_path, RATE, segment_seconds=60)
    sig = ramp(10)
    sp.write(T0, sig)
    sp.flush()
    got = sp.extract(T0 - timedelta(seconds=5), T0 + timedelta(seconds=15))
    assert got.size == 20 * RATE
    assert not got[: 5 * RATE].any()
    assert not got[15 * RATE :].any()
    np.testing.assert_allclose(got[5 * RATE : 15 * RATE], sig, atol=1 / 32768)


def test_spool_survives_reopen(tmp_path):
    sp = Spool(tmp_path, RATE, segment_seconds=60)
    sp.write(T0, ramp(10))
    sp.flush()
    again = Spool(tmp_path, RATE, segment_seconds=60)
    assert again.extract(T0, T0 + timedelta(seconds=10)).size == 10 * RATE


def test_capture_reads_blocks(monkeypatch):
    class FakeStream:
        def __init__(self, callback, **kwargs):
            self.callback = callback
            self.kwargs = kwargs

        def start(self):
            self.callback(np.ones((1600, 1), dtype=np.float32), 1600, None, None)

        def stop(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(audio_mod.sd, "InputStream", FakeStream)
    cap = Capture(RATE, None)
    cap.start()
    block = cap.read(timeout=0.1)
    assert block is not None and block.shape == (1600,)
    assert cap.read(timeout=0.01) is None
    cap.stop()


@pytest.mark.parametrize("seconds", [0.5, 61.3])
def test_spool_segment_names_sort_by_time(tmp_path, seconds):
    sp = Spool(tmp_path, RATE, segment_seconds=60)
    sp.write(T0, ramp(seconds))
    sp.flush()
    names = sorted(p.name for p in tmp_path.glob("*.flac"))
    assert names[0].startswith("20261108T120000")

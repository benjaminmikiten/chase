from datetime import timedelta

from chase.model import Episode, Violation
from chase.store import BarkLog
from tests.conftest import T0, make_windows


def test_windows_round_trip(tmp_path):
    log = BarkLog(tmp_path / "chase.sqlite")
    ws = make_windows(T0, 5.0)
    for i, w in enumerate(ws):
        log.add_window(w, burst_start=(i == 0))
    got = log.windows_between(T0, T0 + timedelta(minutes=1))
    assert got == ws
    assert log.last_window_end() == ws[-1].end
    assert log.burst_starts_between(T0, T0 + timedelta(minutes=1)) == [T0]


def test_add_window_is_idempotent(tmp_path):
    log = BarkLog(tmp_path / "chase.sqlite")
    w = make_windows(T0, 1.0)[0]
    log.add_window(w, burst_start=True)
    log.add_window(w, burst_start=True)
    assert len(log.windows_between(T0, T0 + timedelta(minutes=1))) == 1


def test_empty_log(tmp_path):
    log = BarkLog(tmp_path / "chase.sqlite")
    assert log.last_window_end() is None
    assert log.windows_between(T0, T0 + timedelta(days=1)) == []


def _episode() -> Episode:
    ws = tuple(make_windows(T0, 3.0))
    return Episode("ep-20261108T120000Z", T0, ws[-1].end, ws, 1, 3.0, 0.0, -20.0, 1)


def test_episode_round_trip_and_dedupe(tmp_path):
    log = BarkLog(tmp_path / "chase.sqlite")
    e = _episode()
    assert log.save_episode(e) is True
    assert log.save_episode(e) is False
    assert log.episodes_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1)) == [e]


def test_latest_violation_revision(tmp_path):
    log = BarkLog(tmp_path / "chase.sqlite")
    v1 = Violation("v-1", T0, T0 + timedelta(hours=24), ("a", "b"), 1)
    v2 = Violation("v-1", T0, T0 + timedelta(hours=24), ("a", "b", "c"), 2)
    assert log.save_violation(v1) is True
    assert log.save_violation(v2) is True
    assert log.save_violation(v2) is False
    assert log.latest_violations(T0) == [v2]
    assert log.latest_violations(T0 + timedelta(hours=25)) == []


def test_persists_across_instances(tmp_path):
    path = tmp_path / "chase.sqlite"
    BarkLog(path).save_episode(_episode())
    assert len(BarkLog(path).episodes_between(T0, T0 + timedelta(hours=1))) == 1

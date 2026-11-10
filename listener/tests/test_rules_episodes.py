import random
from datetime import timedelta

import pytest

from chase.rules import count_bursts, current_run, episode_id, finalized, find_episodes
from tests.conftest import T0, make_windows

HOP = 0.48
MIN = 60.0


def test_continuous_11_minutes_is_one_episode(sa_rules):
    eps = find_episodes(make_windows(T0, 11 * MIN), sa_rules, HOP)
    assert len(eps) == 1
    assert eps[0].id == "ep-20261108T120000Z"
    assert eps[0].start == T0
    assert eps[0].merged_from == 1
    assert eps[0].barking_seconds == pytest.approx(11 * MIN, abs=1)


def test_episode_id_format():
    assert episode_id(T0) == "ep-20261108T120000Z"


def test_9_minutes_is_not_an_episode(sa_rules):
    assert find_episodes(make_windows(T0, 9 * MIN), sa_rules, HOP) == []


def _two_parts(gap: float):
    first = make_windows(T0, 6 * MIN)
    second_start = first[-1].end + timedelta(seconds=gap)
    return first + make_windows(second_start, 6 * MIN)


def test_gap_29s_keeps_run(sa_rules):
    eps = find_episodes(_two_parts(29), sa_rules, HOP)
    assert len(eps) == 1
    assert eps[0].longest_gap_seconds == pytest.approx(29, abs=0.01)


def test_gap_31s_breaks_run(sa_rules):
    assert find_episodes(_two_parts(31), sa_rules, HOP) == []


def _two_episodes(separation_minutes: float):
    first = make_windows(T0, 11 * MIN)
    second_start = first[-1].end + timedelta(minutes=separation_minutes)
    return first + make_windows(second_start, 11 * MIN)


def test_two_episodes_9min_apart_merge(sa_rules):
    eps = find_episodes(_two_episodes(9), sa_rules, HOP)
    assert len(eps) == 1
    assert eps[0].merged_from == 2
    assert eps[0].longest_gap_seconds == pytest.approx(540, abs=0.01)


def test_two_episodes_11min_apart_stay_separate(sa_rules):
    assert len(find_episodes(_two_episodes(11), sa_rules, HOP)) == 2


def test_unsorted_and_overlapping_windows(sa_rules):
    windows = _two_episodes(11)
    expected = find_episodes(windows, sa_rules, HOP)
    shuffled = windows + windows[::7]  # duplicates
    random.Random(1).shuffle(shuffled)
    got = find_episodes(shuffled, sa_rules, HOP)
    assert [(e.start, e.end) for e in got] == [(e.start, e.end) for e in expected]
    assert all(e.longest_gap_seconds >= 0 for e in got)


def test_bursts():
    windows = make_windows(T0, 3.0) + make_windows(T0 + timedelta(seconds=5.0), 3.0)
    assert count_bursts(windows, HOP) == 2


def test_bark_count_on_episode(sa_rules):
    windows = make_windows(T0, 11 * MIN, gap_every=5.0, gap_len=2.0)
    eps = find_episodes(windows, sa_rules, HOP)
    assert len(eps) == 1
    assert eps[0].bark_count == count_bursts(windows, HOP)
    assert eps[0].bark_count > 50


def test_finalized_waits_for_separation(sa_rules):
    eps = find_episodes(make_windows(T0, 11 * MIN), sa_rules, HOP)
    end = eps[0].end
    assert finalized(eps, end + timedelta(minutes=9), sa_rules) == []
    assert finalized(eps, end + timedelta(minutes=10), sa_rules) == eps


def test_current_run_in_progress(sa_rules):
    windows = make_windows(T0, 462)
    last_end = windows[-1].end
    status = current_run(windows, last_end + timedelta(seconds=5), sa_rules)
    assert status is not None
    assert status.start == T0
    assert status.duration_seconds == pytest.approx(462, abs=1)
    assert status.seconds_until_episode == pytest.approx(138, abs=1)


def test_current_run_over_after_gap(sa_rules):
    windows = make_windows(T0, 462)
    assert current_run(windows, windows[-1].end + timedelta(seconds=31), sa_rules) is None


def test_current_run_empty(sa_rules):
    assert current_run([], T0, sa_rules) is None


def test_finalized_blocked_by_open_run_within_separation(sa_rules):
    eps = find_episodes(make_windows(T0, 11 * MIN), sa_rules, HOP)
    end = eps[0].end
    open_start = end + timedelta(minutes=8)  # a new run began before end + min_separation
    now = end + timedelta(minutes=10)
    assert finalized(eps, now, sa_rules, open_run_start=open_start) == []
    later = end + timedelta(minutes=11)  # run started after the separation window: no merge
    assert finalized(eps, now, sa_rules, open_run_start=later) == eps

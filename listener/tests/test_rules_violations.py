import random
from dataclasses import replace
from datetime import timedelta

from chase.model import Episode
from chase.rules import assign_violations, episode_id, violation_id
from tests.conftest import T0


def ep(hours: float, minutes: float = 11) -> Episode:
    start = T0 + timedelta(hours=hours)
    return Episode(
        id=episode_id(start),
        start=start,
        end=start + timedelta(minutes=minutes),
        windows=(),
        bark_count=100,
        barking_seconds=minutes * 60,
        longest_gap_seconds=5,
        mean_loudness_dbfs=-20,
        merged_from=1,
    )


def ids(*eps: Episode) -> tuple[str, ...]:
    return tuple(e.id for e in eps)


def test_violation_id_format():
    assert violation_id(T0) == "v-20261108T120000Z"


def test_two_within_24h_make_violation(sa_rules):
    a, b = ep(0), ep(10)
    out = assign_violations([], [a, b], sa_rules)
    assert len(out) == 1
    v = out[0]
    assert v.id == violation_id(a.start)
    assert v.episode_ids == ids(a, b)
    assert v.revision == 1
    assert v.window_start == a.start
    assert v.window_end == a.start + timedelta(hours=24)


def test_third_inside_window_bumps_revision(sa_rules):
    a, b, c = ep(0), ep(10), ep(20)
    first = assign_violations([], [a, b], sa_rules)
    out = assign_violations(first, [a, b, c], sa_rules)
    assert len(out) == 1
    assert out[0].episode_ids == ids(a, b, c)
    assert out[0].revision == 2


def test_all_at_once_counts_revisions(sa_rules):
    out = assign_violations([], [ep(0), ep(10), ep(20)], sa_rules)
    assert out[0].revision == 2


def test_episode_after_window_starts_fresh(sa_rules):
    a, b, c, d = ep(0), ep(10), ep(30), ep(40)
    out = assign_violations([], [a, b, c], sa_rules)
    assert [v.episode_ids for v in out] == [ids(a, b)]
    out = assign_violations(out, [a, b, c, d], sa_rules)
    assert [v.episode_ids for v in out] == [ids(a, b), ids(c, d)]


def test_far_apart_never_violate(sa_rules):
    assert assign_violations([], [ep(0), ep(30)], sa_rules) == []


def test_pool_drops_stale_entries(sa_rules):
    a, b, c = ep(0), ep(30), ep(40)
    out = assign_violations([], [a, b, c], sa_rules)
    assert [v.episode_ids for v in out] == [ids(b, c)]


def test_idempotent(sa_rules):
    eps = [ep(0), ep(10), ep(20), ep(30), ep(40)]
    out = assign_violations([], eps, sa_rules)
    assert assign_violations(out, eps, sa_rules) == out


def test_episode_never_in_two_violations(sa_rules):
    rng = random.Random(42)
    for _ in range(200):
        hours = sorted({round(rng.uniform(0, 120), 1) for _ in range(rng.randint(0, 12))})
        eps = [ep(h) for h in hours]
        # Feed incrementally, like the service does.
        out = []
        for i in range(len(eps)):
            out = assign_violations(out, eps[: i + 1], sa_rules)
        all_ids = [i for v in out for i in v.episode_ids]
        assert len(all_ids) == len(set(all_ids))
        assert out == assign_violations([], eps, sa_rules)


def test_threshold_of_three(sa_rules):
    rules = replace(sa_rules, episodes_per_window=3)
    a, b, c = ep(0), ep(1), ep(2)
    assert assign_violations([], [a, b], rules) == []
    out = assign_violations([], [a, b, c], rules)
    assert [v.episode_ids for v in out] == [ids(a, b, c)]

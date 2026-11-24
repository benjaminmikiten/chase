import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest

from chase.config import RuleConfig
from chase.live import STALE_SECONDS, LiveStatus, serve
from chase.rules import current_run
from tests.conftest import T0, make_windows


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def feed(live: LiveStatus, clock: Clock, windows) -> None:
    """Feed windows the way the service does: score each, then log it as a bark."""
    for w in windows:
        clock.t = w.end
        live.scored(w.end, w.confidence)
        live.bark(w)


def test_no_audio_until_something_is_scored(sa_rules: RuleConfig):
    snap = LiveStatus(sa_rules, Clock(T0)).snapshot()
    assert snap["state"] == "no_audio"
    assert snap["run"] is None
    assert snap["last_bark_end"] is None


def test_quiet_when_scoring_but_never_barked(sa_rules: RuleConfig):
    clock = Clock(T0)
    live = LiveStatus(sa_rules, clock)
    live.scored(T0, 0.05)
    snap = live.snapshot()
    assert snap["state"] == "quiet"
    assert snap["last_score"] == 0.05


def test_barking_reports_run_length_and_time_to_episode(sa_rules: RuleConfig):
    clock = Clock(T0)
    live = LiveStatus(sa_rules, clock)
    windows = make_windows(T0, 120)
    feed(live, clock, windows)

    snap = live.snapshot()
    run = current_run(windows, clock.t, sa_rules)
    assert run is not None
    assert snap["state"] == "barking"
    assert snap["run"]["duration_seconds"] == pytest.approx(run.duration_seconds, abs=0.1)
    assert snap["run"]["seconds_until_episode"] == pytest.approx(
        sa_rules.episode_min_minutes * 60 - run.duration_seconds, abs=0.1
    )
    assert snap["run"]["is_episode"] is False
    assert snap["run"]["start"] == "2026-11-08T12:00:00Z"


def test_pause_inside_the_gap_keeps_the_run(sa_rules: RuleConfig):
    clock = Clock(T0)
    live = LiveStatus(sa_rules, clock)
    feed(live, clock, make_windows(T0, 60))
    end = clock.t
    clock.t = end + timedelta(seconds=10)
    live.scored(clock.t, 0.1)

    snap = live.snapshot()
    assert snap["state"] == "paused"
    assert snap["seconds_since_bark"] == pytest.approx(10, abs=0.1)
    assert snap["run"]["resumes_within_seconds"] == pytest.approx(
        sa_rules.max_gap_seconds - 10, abs=0.1
    )


def test_quiet_after_the_gap_closes_and_remembers_last_bark(sa_rules: RuleConfig):
    clock = Clock(T0)
    live = LiveStatus(sa_rules, clock)
    feed(live, clock, make_windows(T0, 60))
    end = clock.t
    clock.t = end + timedelta(seconds=sa_rules.max_gap_seconds + 5)
    live.scored(clock.t, 0.1)

    snap = live.snapshot()
    assert snap["state"] == "quiet"
    assert snap["run"] is None
    assert snap["last_bark_end"] is not None
    assert snap["seconds_since_bark"] == pytest.approx(sa_rules.max_gap_seconds + 5, abs=0.1)


def test_barking_after_a_long_gap_starts_a_new_run(sa_rules: RuleConfig):
    clock = Clock(T0)
    live = LiveStatus(sa_rules, clock)
    feed(live, clock, make_windows(T0, 300))
    later = T0 + timedelta(minutes=30)
    feed(live, clock, make_windows(later, 20))

    snap = live.snapshot()
    assert snap["run"]["start"] == "2026-11-08T12:30:00Z"
    assert snap["run"]["duration_seconds"] < 21


def test_agrees_with_the_rule_engine_across_short_gaps(sa_rules: RuleConfig):
    """Gaps under max_gap stay one run; the live length matches current_run over everything."""
    clock = Clock(T0)
    live = LiveStatus(sa_rules, clock)
    windows = make_windows(T0, 900, gap_every=20, gap_len=sa_rules.max_gap_seconds - 5)
    feed(live, clock, windows)

    run = current_run(windows, clock.t, sa_rules)
    assert run is not None
    snap = live.snapshot()
    assert snap["run"]["duration_seconds"] == pytest.approx(run.duration_seconds, abs=0.1)
    assert snap["run"]["is_episode"] is (run.seconds_until_episode == 0)


def test_history_seeds_a_run_in_progress(sa_rules: RuleConfig):
    """After a restart mid-bout, the page shows the bout's real length, not time since restart."""
    history = make_windows(T0, 400)
    clock = Clock(history[-1].end + timedelta(seconds=2))
    live = LiveStatus(sa_rules, clock, history=reversed(history))  # order must not matter
    live.scored(clock.t, 0.9)

    snap = live.snapshot()
    assert snap["run"]["start"] == "2026-11-08T12:00:00Z"
    assert snap["run"]["duration_seconds"] == pytest.approx(
        (history[-1].end - T0).total_seconds(), abs=0.1
    )


def test_stale_audio_reports_no_audio_even_mid_run(sa_rules: RuleConfig):
    clock = Clock(T0)
    live = LiveStatus(sa_rules, clock)
    feed(live, clock, make_windows(T0, 60))
    clock.t += timedelta(seconds=STALE_SECONDS + 1)

    snap = live.snapshot()
    assert snap["state"] == "no_audio"
    assert snap["run"] is not None  # the bout is still open; we just can't hear right now


def test_http_serves_page_and_json(sa_rules: RuleConfig):
    clock = Clock(T0)
    live = LiveStatus(sa_rules, clock)
    feed(live, clock, make_windows(T0, 30))
    server = serve(live, 0, host="127.0.0.1")
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/") as r:
            assert r.headers["Content-Type"].startswith("text/html")
            assert b"/api/live" in r.read()
        with urllib.request.urlopen(base + "/api/live") as r:
            data = json.loads(r.read())
        assert data["state"] == "barking"
        assert data["rules"]["episode_min_minutes"] == sa_rules.episode_min_minutes
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/nope")
        assert err.value.code == 404
    finally:
        server.shutdown()
        server.server_close()

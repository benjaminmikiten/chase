import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from chase.day import summarize
from chase.live import LiveStatus, serve
from chase.model import Episode, Violation
from chase.store import BarkLog
from tests.conftest import make_windows

TZ = ZoneInfo("America/Chicago")
# 10pm to 10pm Central: 04:00Z Nov 8 to 04:00Z Nov 9.
START = datetime(2026, 11, 7, 22, 0, tzinfo=TZ)
END = START + timedelta(days=1)


def bark(log: BarkLog, at: datetime, seconds: float = 3.0) -> None:
    for i, w in enumerate(make_windows(at, seconds)):
        log.add_window(w, burst_start=i == 0)


@pytest.fixture
def log(tmp_path):
    log = BarkLog(tmp_path / "chase.sqlite")
    yield log
    log.close()


def test_counts_bursts_by_local_hour_and_against_the_30_days_before(log):
    bark(log, START + timedelta(minutes=5))  # 10pm hour
    bark(log, START + timedelta(minutes=50))  # still 10pm hour
    bark(log, START + timedelta(hours=5, minutes=1))  # 3am
    bark(log, END)  # the next window, not this one
    for d in range(1, 31, 2):  # 15 bursts in the 30 days before
        bark(log, START - timedelta(days=d))
    s = summarize(log, START, END, TZ)
    assert s["schema_version"] == 1 and s["timezone"] == "America/Chicago"
    assert s["barks"] == 3 and s["typical_barks"] == 0.5
    assert len(s["hours"]) == 24
    assert s["hours"][0] == {"start": "2026-11-07T22:00:00-06:00", "barks": 2}
    assert s["hours"][5]["barks"] == 1 and sum(h["barks"] for h in s["hours"]) == 3
    assert s["barking_seconds"] > 8


def test_dst_night_has_25_hours():
    start = datetime(2026, 10, 31, 22, 0, tzinfo=TZ)  # DST ends Nov 1, 2am
    end = datetime(2026, 11, 1, 22, 0, tzinfo=TZ)
    s = summarize(FakeLog(), start, end, TZ)
    assert len(s["hours"]) == 25 and s["barking_seconds"] == 0.0


def test_lists_episodes_and_overlapping_violations(log):
    ws = tuple(make_windows(START + timedelta(hours=3), 600))
    ep = Episode("ep-1", ws[0].start, ws[-1].end, ws, 40, 590.0, 2.0, -18.3, 1)
    log.save_episode(ep)
    log.save_violation(Violation("v-1", START - timedelta(hours=10), START + timedelta(hours=14),
                                 ("ep-0", "ep-1"), 2))  # fmt: skip
    log.save_violation(Violation("v-old", START - timedelta(days=3), START - timedelta(days=2),
                                 ("ep-x",), 1))  # fmt: skip
    s = summarize(log, START, END, TZ)
    assert [e["id"] for e in s["episodes"]] == ["ep-1"]
    assert s["episodes"][0]["bark_count"] == 40 and s["episodes"][0]["duration_seconds"] > 590
    assert s["violations"] == [{"id": "v-1", "window_start": "2026-11-07T18:00:00Z",
                                "window_end": "2026-11-08T18:00:00Z", "episodes": 2,
                                "revision": 2}]  # fmt: skip


@pytest.mark.parametrize("start,end", [(END, START), (START, START + timedelta(days=8)),
                                       (START.replace(tzinfo=None), END)])  # fmt: skip
def test_rejects_bad_spans(start, end):
    with pytest.raises(ValueError):
        summarize(FakeLog(), start, end, TZ)


class FakeLog:
    def burst_starts_between(self, start, end):
        return []

    def windows_between(self, start, end):
        return []

    def episodes_between(self, start, end):
        return []

    def latest_violations(self, since):
        return []


def test_http_endpoint(sa_rules):
    seen = []

    def day(start, end):
        seen.append((start, end))
        return summarize(FakeLog(), start, end, TZ)

    live = LiveStatus(sa_rules, lambda: datetime(2026, 11, 8, tzinfo=UTC))
    server = serve(live, 0, host="127.0.0.1", day=day)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        q = "start=2026-11-08T04:00:00Z&end=2026-11-08T22:00:00-06:00"
        body = json.load(urllib.request.urlopen(f"{base}/api/day?{q}"))
        assert body["barks"] == 0 and body["live_state"] == "no_audio"
        assert seen[0] == (datetime(2026, 11, 8, 4, tzinfo=UTC),
                           datetime(2026, 11, 9, 4, tzinfo=UTC))  # fmt: skip
        with pytest.raises(urllib.error.HTTPError) as bad:
            urllib.request.urlopen(f"{base}/api/day?start=yesterday&end=today")
        assert bad.value.code == 400 and "ISO" in json.load(bad.value)["error"]
    finally:
        server.shutdown()

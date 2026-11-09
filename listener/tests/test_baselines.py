from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from chase.baselines import compute_baselines, hourly_history

TZ = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")


def local(*args) -> datetime:
    return datetime(*args, tzinfo=TZ).astimezone(UTC)


def test_typical_this_hour():
    now = local(2026, 11, 20, 7, 30)
    bursts = []
    for d in range(1, 31):
        day = datetime(2026, 11, 20, 7, 0, tzinfo=TZ) - timedelta(days=d)
        bursts += [(day + timedelta(minutes=m)).astimezone(UTC) for m in range(10)]
    bursts += [local(2026, 11, 20, 7, 5), local(2026, 11, 20, 7, 6)]
    b = compute_baselines(bursts, now, TZ)
    assert b.typical_this_hour == pytest.approx(10.0)
    assert b.barks_this_hour == 2
    assert b.barks_today == 2
    assert b.daily_average == pytest.approx(10.0)


def test_today_bucket_is_local():
    # 05:30 UTC on Nov 9 is 23:30 CST on Nov 8.
    burst = datetime(2026, 11, 9, 5, 30, tzinfo=UTC)
    b = compute_baselines([burst], datetime(2026, 11, 9, 5, 45, tzinfo=UTC), TZ)
    assert b.barks_today == 1
    b = compute_baselines([burst], datetime(2026, 11, 9, 6, 15, tzinfo=UTC), TZ)
    assert b.barks_today == 0  # now it's Nov 9 local


def test_dst_fall_back():
    # 2026-11-01 01:30 happens twice in Chicago: 06:30 UTC (CDT) and 07:30 UTC (CST).
    first = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    second = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)
    now = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)
    b = compute_baselines([first, second], now, TZ)
    # Five Sundays in the 30 prior days (Oct 4 .. Nov 1): average per Sunday at 01:xx.
    assert b.heatmap[(6, 1)] == pytest.approx(2 / 5)
    hist = hourly_history([first, second], now, TZ)
    assert sum(h["barks"] for h in hist) == 2


def test_heatmap_keys():
    b = compute_baselines([], local(2026, 11, 20, 7, 30), TZ)
    assert len(b.heatmap) == 168
    assert set(b.heatmap.values()) == {0.0}
    assert b.typical_this_hour == 0.0


def test_hourly_history_zero_filled():
    now = local(2026, 11, 20, 7, 30)
    burst = local(2026, 11, 19, 15, 10)
    hist = hourly_history([burst], now, TZ)
    assert len(hist) == 30 * 24
    hit = [h for h in hist if h["barks"]]
    assert hit == [{"hour_start": "2026-11-19T21:00:00Z", "barks": 1}]
    assert all(h["hour_start"].endswith("Z") for h in hist)


def test_hourly_history_across_fall_back():
    now = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)  # window includes Nov 1
    hist = hourly_history([], now, TZ)
    assert len(hist) == 30 * 24  # absolute hours: DST can't add or drop a bucket
    assert hist[-1]["hour_start"] == "2026-11-20T18:00:00Z"  # includes the current hour

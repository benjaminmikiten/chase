from prometheus_client import CollectorRegistry, generate_latest

from chase.baselines import Baselines
from chase.metrics import Metrics

PINNED = [
    "chase_barking_windows_total",
    "chase_barks_total",
    "chase_episodes_total",
    "chase_violations_total",
    "chase_audio_errors_total",
    "chase_current_run_seconds",
    "chase_seconds_until_episode",
    "chase_barking_now",
    "chase_in_episode",
    "chase_episodes_in_window",
    "chase_episodes_per_window",
    "chase_barks_this_hour",
    "chase_typical_barks_this_hour",
    "chase_barks_today",
    "chase_daily_average_barks",
    "chase_heatmap_barks",
    "chase_outbox_pending",
    "chase_heartbeat_timestamp_seconds",
    "chase_data_free_bytes",
    "chase_last_tick_success_timestamp_seconds",
    "chase_clock_corrections_total",
    "chase_bark_confidence",
    "chase_bark_loudness_dbfs",
]


def test_pinned_names_exported():
    reg = CollectorRegistry()
    m = Metrics(reg)
    m.audio_errors.labels(reason="capture").inc(0)
    m.heatmap.labels(weekday="0", hour="0").set(0)
    text = generate_latest(reg).decode()
    for name in PINNED:
        assert f"\n{name}" in "\n" + text, name


def test_apply_baselines():
    reg = CollectorRegistry()
    m = Metrics(reg)
    heat = {(wd, h): float(wd * 24 + h) for wd in range(7) for h in range(24)}
    m.apply_baselines(Baselines(4, 2.5, 30, 12.0, heat))
    assert reg.get_sample_value("chase_barks_this_hour") == 4
    assert reg.get_sample_value("chase_typical_barks_this_hour") == 2.5
    assert reg.get_sample_value("chase_barks_today") == 30
    assert reg.get_sample_value("chase_daily_average_barks") == 12.0
    assert reg.get_sample_value("chase_heatmap_barks", {"weekday": "6", "hour": "23"}) == 167.0
    series = [s for s in reg.collect() if s.name == "chase_heatmap_barks"][0].samples
    assert len(series) == 168

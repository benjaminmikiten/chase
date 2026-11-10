"""Prometheus metrics. Names are pinned: grafana/dashboard.json depends on them."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

from chase.baselines import Baselines


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None):
        r = self.registry = registry or CollectorRegistry()
        self.barking_windows = Counter(
            "chase_barking_windows", "Audio windows classified as barking", registry=r
        )
        self.barks = Counter("chase_barks", "Estimated barking bursts", registry=r)
        self.episodes = Counter("chase_episodes", "Finalized barking episodes", registry=r)
        self.violations = Counter("chase_violations", "New violations", registry=r)
        self.audio_errors = Counter("chase_audio_errors", "Errors by stage", ["reason"], registry=r)
        self.current_run = Gauge(
            "chase_current_run_seconds", "Length of barking in progress", registry=r
        )
        self.until_episode = Gauge(
            "chase_seconds_until_episode", "Seconds until the current run counts", registry=r
        )
        self.barking_now = Gauge("chase_barking_now", "1 while barking is in progress", registry=r)
        self.in_episode = Gauge(
            "chase_in_episode", "1 while the current run is episode-length", registry=r
        )
        self.episodes_in_window = Gauge(
            "chase_episodes_in_window", "Episodes in the current violation window", registry=r
        )
        self.episodes_per_window = Gauge(
            "chase_episodes_per_window", "Episodes needed for a violation", registry=r
        )
        self.barks_this_hour = Gauge("chase_barks_this_hour", "Barks this local hour", registry=r)
        self.typical_this_hour = Gauge(
            "chase_typical_barks_this_hour", "30-day average for this hour", registry=r
        )
        self.barks_today = Gauge("chase_barks_today", "Barks today (local)", registry=r)
        self.daily_average = Gauge(
            "chase_daily_average_barks", "30-day average barks per day", registry=r
        )
        self.heatmap = Gauge(
            "chase_heatmap_barks",
            "30-day average barks by weekday (0=Mon) and hour",
            ["weekday", "hour"],
            registry=r,
        )
        self.outbox_pending = Gauge(
            "chase_outbox_pending", "Evidence entries waiting to upload", registry=r
        )
        self.heartbeat = Gauge(
            "chase_heartbeat_timestamp_seconds", "Last time audio was processed", registry=r
        )
        self.data_free = Gauge("chase_data_free_bytes", "Free space on the data volume", registry=r)
        self.last_tick_success = Gauge(
            "chase_last_tick_success_timestamp_seconds",
            "Last time the rules/evidence tick completed",
            registry=r,
        )
        self.clock_corrections = Counter(
            "chase_clock_corrections",
            "Times audio timestamps were re-anchored to the system clock",
            registry=r,
        )
        self.confidence = Histogram(
            "chase_bark_confidence",
            "YAMNet score of barking windows",
            buckets=[round(0.5 + 0.05 * i, 2) for i in range(11)],
            registry=r,
        )
        self.loudness = Histogram(
            "chase_bark_loudness_dbfs",
            "Loudness of barking windows",
            buckets=list(range(-80, 1, 5)),
            registry=r,
        )

    def apply_baselines(self, b: Baselines) -> None:
        self.barks_this_hour.set(b.barks_this_hour)
        self.typical_this_hour.set(b.typical_this_hour)
        self.barks_today.set(b.barks_today)
        self.daily_average.set(b.daily_average)
        for (wd, h), v in b.heatmap.items():
            self.heatmap.labels(weekday=str(wd), hour=str(h)).set(v)

    def serve(self, port: int) -> None:
        start_http_server(port, registry=self.registry)

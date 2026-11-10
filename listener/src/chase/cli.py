"""Command line: `chase run` and `chase replay`."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from chase.config import Settings, load_preset, load_settings
from chase.rules import assign_violations, find_episodes
from chase.store import BarkLog

DEFAULT_PRESETS = Path("/config/presets")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chase", description="Dog-bark monitor")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="listen and detect (the long-running service)")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--presets", type=Path, default=DEFAULT_PRESETS)

    rep = sub.add_parser("replay", help="re-run the rules over logged barks (read-only)")
    rep.add_argument("--config", type=Path, required=True)
    rep.add_argument("--presets", type=Path, default=DEFAULT_PRESETS)
    rep.add_argument("--preset", type=Path, help="try a different preset file")
    rep.add_argument("--since", type=date.fromisoformat, help="local date, YYYY-MM-DD")
    rep.add_argument("--until", type=date.fromisoformat, help="local date, YYYY-MM-DD (inclusive)")
    return p


def _fmt(d: datetime, tz: ZoneInfo) -> str:
    return d.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def replay(settings: Settings, preset: Path | None, since: date | None, until: date | None) -> int:
    rules = load_preset(preset) if preset else settings.rules
    tz = ZoneInfo(settings.timezone)
    start = (
        datetime.combine(since, time.min, tz).astimezone(UTC)
        if since
        else datetime(1970, 1, 1, tzinfo=UTC)
    )
    end = (
        datetime.combine(until + timedelta(days=1), time.min, tz).astimezone(UTC)
        if until
        else datetime.now(UTC) + timedelta(days=1)
    )
    db = settings.data_dir / "chase.sqlite"
    windows = []
    if db.exists():
        log = BarkLog(db, read_only=True)
        windows = log.windows_between(start, end)
        log.close()

    episodes = find_episodes(windows, rules, settings.hop_seconds)
    violations = assign_violations([], episodes, rules)
    print(f"Rules: {rules.name}  ({len(windows)} barking windows)")
    for e in episodes:
        print(
            f"  episode {e.id}: {_fmt(e.start, tz)} - {_fmt(e.end, tz)}, "
            f"{e.duration_seconds / 60:.1f} min, {e.barking_seconds / 60:.1f} min barking, "
            f"~{e.bark_count} bursts, merged_from={e.merged_from}"
        )
    for v in violations:
        print(f"  violation {v.id} (rev {v.revision}): {', '.join(v.episode_ids)}")
    print(f"{len(episodes)} episodes, {len(violations)} violations")
    return 0


def run(settings: Settings) -> int:
    if settings.audio_device is None:
        print(
            "audio_device must be set in chase.yaml (a name from `python -m sounddevice` or "
            "`arecord -l`, e.g. 'USB'). The system default is usually the built-in input, "
            "not your USB mic.",
            file=sys.stderr,
        )
        return 2
    from chase.audio import Capture, Spool
    from chase.detector import BarkDetector
    from chase.events import emit
    from chase.metrics import Metrics
    from chase.service import Service

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    metrics = Metrics()
    metrics.serve(settings.metrics_port)
    log = BarkLog(settings.data_dir / "chase.sqlite")
    service = Service(
        settings,
        log,
        BarkDetector(settings.model_path, settings.class_map_path),
        Spool(settings.data_dir / "spool", settings.capture_rate),
        metrics,
    )
    emit("listener_started", rules=settings.rules.name, capture_rate=settings.capture_rate)
    try:
        service.run(Capture(settings.capture_rate, settings.audio_device), stop=stop)
    finally:
        log.close()
        emit("listener_stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config, args.presets)
    if args.command == "replay":
        return replay(settings, args.preset, args.since, args.until)
    return run(settings)

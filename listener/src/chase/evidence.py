"""Evidence outbox (uploaded by Plan 2's uploader) and the local-only v2 backlog.

Outbox layout (schema_version 1):
    outbox/episodes/<episode-id>/audio.flac
    outbox/episodes/<episode-id>/metadata.json
    outbox/violations/<violation-id>/rev-<n>.json

Every write goes to a temp path and is renamed into place, so a crash never leaves a
half-written entry, and rewriting an existing entry is a no-op.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from chase.config import RuleConfig
from chase.model import Episode, Violation

SCHEMA_VERSION = 1
_BACKLOG_NAME = "%Y%m%dT%H%M%S.%fZ"


def iso(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S") + (
        f".{d.microsecond:06d}Z" if d.microsecond else "Z"
    )


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


Gap = tuple[datetime, datetime]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_episode(
    outbox: Path,
    episode: Episode,
    render: Callable[[Path], list[Gap]],
    rate: int,
    clip_start: datetime,
    device_label: str,
    rules: RuleConfig,
    model_name: str,
) -> Path:
    final = outbox / "episodes" / episode.id
    if final.exists():
        return final
    tmp = final.with_name(episode.id + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    flac = tmp / "audio.flac"
    gaps = render(flac)  # streams the clip to disk; never holds a long episode in memory
    duration = sf.info(str(flac)).frames / rate
    meta = {
        "schema_version": SCHEMA_VERSION,
        "episode_id": episode.id,
        "start": iso(episode.start),
        "end": iso(episode.end),
        "clip_start": iso(clip_start),
        "clip_end": iso(clip_start + timedelta(seconds=duration)),
        "barking_seconds": episode.barking_seconds,
        "barking_bursts_estimated": episode.bark_count,
        "longest_gap_seconds": episode.longest_gap_seconds,
        "mean_loudness_dbfs": episode.mean_loudness_dbfs,
        "merged_from": episode.merged_from,
        "windows": [
            {
                "start": iso(w.start),
                "end": iso(w.end),
                "confidence": w.confidence,
                "loudness_dbfs": w.loudness_dbfs,
            }
            for w in episode.windows
        ],
        "device": device_label,
        "detector": {"model": model_name, "confidence_threshold": rules.confidence_threshold},
        "audio": {
            "file": "audio.flac",
            "format": "FLAC",
            "sample_rate": rate,
            "duration_seconds": duration,
            "sha256": _sha256(flac),
            "complete": not gaps,
            "gaps": [
                {"start": iso(a), "end": iso(b), "seconds": round((b - a).total_seconds(), 3)}
                for a, b in gaps
            ],
        },
    }
    (tmp / "metadata.json").write_text(json.dumps(meta, indent=2, sort_keys=True))
    os.replace(tmp, final)
    return final


def write_violation(
    outbox: Path, violation: Violation, rules: RuleConfig, hourly_history: list[dict]
) -> Path:
    d = outbox / "violations" / violation.id
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"rev-{violation.revision}.json"
    if path.exists():
        return path
    _write_json_atomic(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "violation_id": violation.id,
            "revision": violation.revision,
            "window_start": iso(violation.window_start),
            "window_end": iso(violation.window_end),
            "episode_ids": list(violation.episode_ids),
            "rule": asdict(rules),
            "hourly_history": hourly_history,
        },
    )
    return path


def write_backlog_clip(
    backlog_dir: Path,
    start: datetime,
    audio: np.ndarray,
    rate: int,
    confidence: float,
    loudness_dbfs: float,
) -> Path:
    """A short local-only clip for the future which-dog classifier, in a per-day folder."""
    utc = start.astimezone(UTC)
    day = backlog_dir / utc.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"{utc.strftime(_BACKLOG_NAME)}.flac"
    sf.write(str(path), audio, rate, subtype="PCM_16", format="FLAC")
    _write_json_atomic(
        path.with_suffix(".json"),
        {
            "start": iso(start),
            "confidence": confidence,
            "loudness_dbfs": loudness_dbfs,
            "sample_rate": rate,
        },
    )
    return path


def prune_backlog(backlog_dir: Path, now: datetime, days: int) -> int:
    """Delete whole day folders older than `days`. Returns the number of clips removed."""
    if not backlog_dir.exists():
        return 0
    cutoff = (now - timedelta(days=days)).astimezone(UTC).date()
    removed = 0
    for day in backlog_dir.iterdir():
        try:
            d = datetime.strptime(day.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day.is_dir() and d < cutoff:
            removed += sum(1 for _ in day.glob("*.flac"))
            shutil.rmtree(day, ignore_errors=True)
    return removed

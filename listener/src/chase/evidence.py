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


def write_episode(
    outbox: Path,
    episode: Episode,
    audio: np.ndarray,
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
    sf.write(str(flac), audio, rate, subtype="PCM_16", format="FLAC")
    duration = audio.size / rate
    meta = {
        "schema_version": SCHEMA_VERSION,
        "episode_id": episode.id,
        "start": iso(episode.start),
        "end": iso(episode.end),
        "clip_start": iso(clip_start),
        "clip_end": iso(clip_start + timedelta(seconds=duration)),
        "barking_seconds": episode.barking_seconds,
        "bark_count_estimated": episode.bark_count,
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
            "sha256": hashlib.sha256(flac.read_bytes()).hexdigest(),
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
    backlog_dir.mkdir(parents=True, exist_ok=True)
    path = backlog_dir / f"{start.astimezone(UTC).strftime(_BACKLOG_NAME)}.flac"
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
    cutoff = now - timedelta(days=days)
    removed = 0
    for p in backlog_dir.glob("*.flac"):
        try:
            start = datetime.strptime(p.stem, _BACKLOG_NAME).replace(tzinfo=UTC)
        except ValueError:
            continue
        if start < cutoff:
            p.unlink(missing_ok=True)
            p.with_suffix(".json").unlink(missing_ok=True)
            removed += 1
    return removed

"""Collect a violation's evidence under evidence/<vid>/ and make forwardable MP3s.

Cloud Run's /tmp is memory, and a merged episode can run for hours, so episodes are handled one
at a time: download the FLAC, encode the MP3 (file to file), hash it, upload it, delete both.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from chase_report.model import EpisodeMeta, Manifest
from chase_report.store import Store


class MissingEvidence(Exception):
    """A manifest references an episode that isn't in the bucket yet; retry later."""


@dataclass(frozen=True)
class EpisodeEvidence:
    meta: EpisodeMeta
    flac_name: str
    mp3_name: str
    mp3_sha256: str
    mp3_bytes: int


@dataclass(frozen=True)
class Evidence:
    manifest: Manifest
    episodes: tuple[EpisodeEvidence, ...]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ffmpeg_mp3(src: Path, dst: Path) -> None:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-ac",
            "1",
            "-b:a",
            "64k",
            str(dst),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")


def assemble(
    store: Store,
    manifest: Manifest,
    workdir: Path,
    encode: Callable[[Path, Path], None] = ffmpeg_mp3,
) -> Evidence:
    vid = manifest.violation_id
    for eid in manifest.episode_ids:  # check everything first: fail before doing any work
        if not store.exists(f"episodes/{eid}/metadata.json") or not store.exists(
            f"episodes/{eid}/audio.flac"
        ):
            raise MissingEvidence(f"{vid}: episode {eid} not uploaded yet")

    out: list[EpisodeEvidence] = []
    for eid in manifest.episode_ids:
        src, dst = f"episodes/{eid}/", f"evidence/{vid}/episodes/{eid}/"
        store.copy(src + "audio.flac", dst + "audio.flac")
        store.copy(src + "metadata.json", dst + "metadata.json")
        meta = EpisodeMeta.from_json(json.loads(store.read_bytes(src + "metadata.json")))

        flac = workdir / f"{eid}.flac"
        mp3 = workdir / f"{eid}.mp3"
        try:
            if store.exists(dst + "audio.mp3"):
                # An earlier revision already produced (and emailed the fingerprint of) this MP3.
                # Reuse it byte for byte; re-encoding could change it under a new ffmpeg build.
                store.download(dst + "audio.mp3", mp3)
                digest, size = sha256_file(mp3), mp3.stat().st_size
            else:
                store.download(src + "audio.flac", flac)
                encode(flac, mp3)
                flac.unlink()
                digest, size = sha256_file(mp3), mp3.stat().st_size
                store.upload(dst + "audio.mp3", mp3, "audio/mpeg")
        finally:
            flac.unlink(missing_ok=True)
            mp3.unlink(missing_ok=True)
        out.append(EpisodeEvidence(meta, dst + "audio.flac", dst + "audio.mp3", digest, size))

    store.copy(
        f"violations/{vid}/rev-{manifest.revision}.json",
        f"evidence/{vid}/rev-{manifest.revision}/manifest.json",
    )
    return Evidence(manifest, tuple(out))

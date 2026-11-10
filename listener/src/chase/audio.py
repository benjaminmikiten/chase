"""Audio capture, windowing, and the on-disk spool of recent audio.

The spool keeps the last `spool_minutes` (and never less than an open run needs) as 1-minute FLAC
segments, so episode clips can include audio from before the episode was recognized.
"""

from __future__ import annotations

import logging
import queue
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)

_BLOCK_SECONDS = 0.1
_NAME_FORMAT = "%Y%m%dT%H%M%S.%fZ"


@dataclass(frozen=True)
class CaptureBlock:
    start: datetime  # wall-clock time of the first sample (system clock, NTP-synced)
    samples: np.ndarray
    overflow: bool  # the driver reported dropped input before this block


def _sounddevice() -> Any:
    """Import sounddevice lazily: importing it initializes PortAudio, which can fail (no audio
    server, mic missing). At capture start that failure is retried and counted; at import time it
    would kill the process."""
    import sounddevice

    return sounddevice


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Capture:
    """Mono float32 blocks from a sounddevice input stream, stamped with wall-clock time.

    The timestamp is taken in the audio callback (not when the block is read), so a slow
    consumer never makes blocks look late.
    """

    def __init__(
        self, rate: int, device: str | int | None, clock: Callable[[], datetime] = _utc_now
    ):
        self.rate = rate
        self.device = device
        self.clock = clock
        self._queue: queue.Queue[CaptureBlock] = queue.Queue()
        self._stream: Any = None

    def _callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        overflow = bool(status and getattr(status, "input_overflow", False))
        if status:
            logger.warning("audio status: %s", status)
        start = self.clock() - timedelta(seconds=frames / self.rate)
        self._queue.put(CaptureBlock(start, indata[:, 0].copy(), overflow))

    def start(self) -> None:
        self._stream = _sounddevice().InputStream(
            samplerate=self.rate,
            channels=1,
            device=self.device,
            callback=self._callback,
            dtype="float32",
            blocksize=int(self.rate * _BLOCK_SECONDS),
        )
        self._stream.start()

    def read(self, timeout: float = 2.0) -> CaptureBlock | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None


class Windower:
    """Overlapping fixed-length windows, timestamped by sample count from t0 (no clock drift)."""

    def __init__(self, rate: int, window_seconds: float, hop_seconds: float, t0: datetime):
        self.rate = rate
        self.window = round(window_seconds * rate)
        self.hop = round(hop_seconds * rate)
        self.t0 = t0
        self._buf = np.zeros(0, dtype=np.float32)
        self._consumed = 0  # samples dropped from the front of _buf since t0

    def shift(self, seconds: float) -> None:
        """Move all future window timestamps by `seconds` (clock re-anchoring)."""
        self.t0 += timedelta(seconds=seconds)

    def _at(self, sample: int) -> datetime:
        return self.t0 + timedelta(seconds=sample / self.rate)

    def push(self, block: np.ndarray) -> list[tuple[datetime, datetime, np.ndarray]]:
        self._buf = np.concatenate([self._buf, block.astype(np.float32, copy=False)])
        out = []
        while self._buf.size >= self.window:
            start = self._consumed
            out.append((self._at(start), self._at(start + self.window), self._buf[: self.window]))
            self._buf = self._buf[self.hop :]
            self._consumed += self.hop
        return out


def spool_keep_after(
    now: datetime, open_run_start: datetime | None, spool_minutes: float, pre_roll_seconds: float
) -> datetime:
    keep = now - timedelta(minutes=spool_minutes)
    if open_run_start is not None:
        keep = min(keep, open_run_start - timedelta(seconds=pre_roll_seconds))
    return keep


@dataclass
class _Segment:
    start: datetime
    frames: int
    path: Path

    def end(self, rate: int) -> datetime:
        return self.start + timedelta(seconds=self.frames / rate)


class Spool:
    def __init__(self, dir: Path, rate: int, segment_seconds: float = 60.0):
        self.dir = dir
        self.rate = rate
        self.segment_frames = round(segment_seconds * rate)
        dir.mkdir(parents=True, exist_ok=True)
        self._segments: list[_Segment] = []
        for p in sorted(dir.glob("*.flac")):
            try:
                start = datetime.strptime(p.stem, _NAME_FORMAT).replace(tzinfo=UTC)
                self._segments.append(_Segment(start, sf.info(str(p)).frames, p))
            except (ValueError, RuntimeError):
                logger.warning("ignoring unreadable spool file %s", p)
        self._cur_start: datetime | None = None
        self._cur: list[np.ndarray] = []
        self._cur_frames = 0

    def _expected_next(self) -> datetime | None:
        if self._cur_start is None:
            return None
        return self._cur_start + timedelta(seconds=self._cur_frames / self.rate)

    def write(self, t: datetime, block: np.ndarray) -> None:
        expected = self._expected_next()
        if expected is not None and abs((t - expected).total_seconds()) > 0.05:
            self.flush()  # discontinuity (restart, dropped audio): start a new segment
        if self._cur_start is None:
            self._cur_start = t
        self._cur.append(block.astype(np.float32, copy=False))
        self._cur_frames += block.size
        if self._cur_frames >= self.segment_frames:
            self.flush()

    def flush(self) -> None:
        if self._cur_start is None or not self._cur:
            return
        data = np.concatenate(self._cur)
        path = self.dir / f"{self._cur_start.strftime(_NAME_FORMAT)}.flac"
        sf.write(str(path), data, self.rate, subtype="PCM_16", format="FLAC")
        self._segments.append(_Segment(self._cur_start, data.size, path))
        self._cur_start, self._cur, self._cur_frames = None, [], 0

    def prune(self, keep_after: datetime) -> int:
        keep, removed = [], 0
        for seg in self._segments:
            if seg.end(self.rate) <= keep_after:
                seg.path.unlink(missing_ok=True)
                removed += 1
            else:
                keep.append(seg)
        self._segments = keep
        return removed

    def extract(self, start: datetime, end: datetime) -> np.ndarray:
        audio, gaps = self._extract(start, end)
        if gaps:
            missing = sum((b - a).total_seconds() for a, b in gaps)
            logger.warning(
                "spool missing %.1fs of audio between %s and %s",
                missing,
                start.isoformat(),
                end.isoformat(),
            )
        return audio

    def _extract(
        self, start: datetime, end: datetime
    ) -> tuple[np.ndarray, list[tuple[datetime, datetime]]]:
        """Audio for [start, end), zero-filled where nothing was recorded, plus those gaps.

        A segment that can't be read (corrupt file) counts as a gap, never an exception.
        """
        n = max(0, round((end - start).total_seconds() * self.rate))
        out = np.zeros(n, dtype=np.float32)
        filled = np.zeros(n, dtype=bool)
        sources: list[tuple[datetime, int, Path | np.ndarray]] = [
            (s.start, s.frames, s.path) for s in self._segments
        ]
        if self._cur_start is not None and self._cur:
            sources.append((self._cur_start, self._cur_frames, np.concatenate(self._cur)))
        for seg_start, frames, src in sources:
            offset = round((seg_start - start).total_seconds() * self.rate)  # seg pos in out
            lo, hi = max(0, offset), min(n, offset + frames)
            if lo >= hi:
                continue
            a, b = lo - offset, hi - offset  # positions within the segment
            if isinstance(src, Path):
                try:
                    data, _ = sf.read(str(src), start=a, stop=b, dtype="float32")
                except (RuntimeError, sf.LibsndfileError):
                    logger.warning("unreadable spool segment %s", src)
                    continue
            else:
                data = src[a:b]
            out[lo : lo + data.size] = data
            filled[lo : lo + data.size] = True
        return out, self._gaps(start, filled)

    def _gaps(self, start: datetime, filled: np.ndarray) -> list[tuple[datetime, datetime]]:
        gaps: list[tuple[datetime, datetime]] = []
        if filled.all():
            return gaps
        edges = np.flatnonzero(np.diff(np.concatenate([[1], filled.astype(np.int8), [1]])))
        for lo, hi in zip(edges[::2], edges[1::2], strict=True):
            gaps.append(
                (
                    start + timedelta(seconds=int(lo) / self.rate),
                    start + timedelta(seconds=int(hi) / self.rate),
                )
            )
        return gaps

    def write_clip(
        self, path: Path, start: datetime, end: datetime, chunk_seconds: float = 60.0
    ) -> list[tuple[datetime, datetime]]:
        """Stream [start, end) into a FLAC file chunk by chunk (bounded memory).

        Returns the spans that had no audio (written as silence), merged across chunks.
        """
        gaps: list[tuple[datetime, datetime]] = []
        step = timedelta(seconds=chunk_seconds)
        with sf.SoundFile(
            str(path), "w", samplerate=self.rate, channels=1, subtype="PCM_16", format="FLAC"
        ) as f:
            t = start
            while t < end:
                chunk_end = min(end, t + step)
                audio, chunk_gaps = self._extract(t, chunk_end)
                f.write(audio)
                for a, b in chunk_gaps:
                    if gaps and gaps[-1][1] == a:
                        gaps[-1] = (gaps[-1][0], b)
                    else:
                        gaps.append((a, b))
                t = chunk_end
        return gaps

"""Audio capture, windowing, and the on-disk spool of recent audio.

The spool keeps the last `spool_minutes` (and never less than an open run needs) as 1-minute FLAC
segments, so episode clips can include audio from before the episode was recognized.
"""

from __future__ import annotations

import logging
import queue
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

logger = logging.getLogger(__name__)

_BLOCK_SECONDS = 0.1
_NAME_FORMAT = "%Y%m%dT%H%M%S.%fZ"


class Capture:
    """Mono float32 blocks from a sounddevice input stream."""

    def __init__(self, rate: int, device: str | int | None):
        self.rate = rate
        self.device = device
        self._queue: queue.Queue[np.ndarray] = queue.Queue()
        self._stream: sd.InputStream | None = None

    def _callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        if status:
            logger.warning("audio status: %s", status)
        self._queue.put(indata[:, 0].copy())

    def start(self) -> None:
        self._stream = sd.InputStream(
            samplerate=self.rate,
            channels=1,
            device=self.device,
            callback=self._callback,
            dtype="float32",
            blocksize=int(self.rate * _BLOCK_SECONDS),
        )
        self._stream.start()

    def read(self, timeout: float = 2.0) -> np.ndarray | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
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
        n = max(0, round((end - start).total_seconds() * self.rate))
        out = np.zeros(n, dtype=np.float32)
        covered = 0
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
                data, _ = sf.read(str(src), start=a, stop=b, dtype="float32")
            else:
                data = src[a:b]
            out[lo:hi] = data
            covered += hi - lo
        if covered < n:
            logger.warning(
                "spool missing %.1fs of audio between %s and %s",
                (n - covered) / self.rate,
                start.isoformat(),
                end.isoformat(),
            )
        return out

"""YAMNet bark detector and loudness."""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable
from math import gcd
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import resample_poly

logger = logging.getLogger(__name__)

TARGET_RATE = 16000
DOG_CLASSES = frozenset({"Dog", "Bark", "Yip", "Howl", "Bow-wow", "Growling"})
_SILENCE_DBFS = -120.0


def load_dog_indices(class_map_path: Path) -> dict[int, str]:
    with open(class_map_path, newline="") as f:
        found = {
            int(row["index"]): row["display_name"]
            for row in csv.DictReader(f)
            if row["display_name"] in DOG_CLASSES
        }
    if not found:
        raise ValueError(f"no dog classes found in {class_map_path}")
    return found


def to_16k(audio: np.ndarray, from_rate: int) -> np.ndarray:
    if from_rate == TARGET_RATE:
        return audio
    g = gcd(TARGET_RATE, from_rate)
    return resample_poly(audio, TARGET_RATE // g, from_rate // g).astype(np.float32)


def loudness_dbfs(audio: np.ndarray) -> float:
    """RMS level relative to full scale (a full-scale sine is about -3 dBFS)."""
    if audio.size == 0:
        return _SILENCE_DBFS
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
    if rms <= 0:
        return _SILENCE_DBFS
    return max(_SILENCE_DBFS, 20.0 * np.log10(rms))


def _litert_interpreter(model_path: str) -> Any:
    from ai_edge_litert.interpreter import Interpreter

    return Interpreter(model_path=model_path)


class BarkDetector:
    """Scores 16 kHz mono audio: max over dog classes of YAMNet's patch-averaged scores."""

    def __init__(
        self,
        model_path: Path,
        class_map_path: Path,
        interpreter_factory: Callable[[str], Any] | None = None,
    ):
        self.model_path = model_path
        self.dog_indices = load_dog_indices(class_map_path)
        self._factory = interpreter_factory or _litert_interpreter
        self._interp: Any = None
        self._fixed_len: int | None = None

    def _interpreter(self) -> Any:
        if self._interp is None:
            logger.info("loading YAMNet model from %s", self.model_path)
            self._interp = self._factory(str(self.model_path))
            self._interp.allocate_tensors()
            shape = self._interp.get_input_details()[0].get("shape")
            # The MediaPipe YAMNet export takes a fixed-length 1-D waveform (15600 samples).
            if shape is not None and len(shape) == 1 and shape[0] > 0:
                self._fixed_len = int(shape[0])
        return self._interp

    def score(self, audio_16k: np.ndarray) -> float:
        interp = self._interpreter()
        x = audio_16k.astype(np.float32, copy=False)
        inp = interp.get_input_details()[0]
        if self._fixed_len is not None:
            n = self._fixed_len
            x = x[:n] if x.size >= n else np.pad(x, (0, n - x.size))
        else:
            interp.resize_tensor_input(inp["index"], x.shape)
            interp.allocate_tensors()
        interp.set_tensor(inp["index"], x)
        interp.invoke()
        scores = np.asarray(interp.get_tensor(interp.get_output_details()[0]["index"]))
        mean = scores.reshape(-1, scores.shape[-1]).mean(axis=0)
        return float(max(mean[i] for i in self.dog_indices if i < mean.size))

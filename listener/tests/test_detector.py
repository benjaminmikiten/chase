import os
from pathlib import Path

import numpy as np
import pytest

from chase.detector import BarkDetector, load_dog_indices, loudness_dbfs, to_16k


@pytest.fixture
def class_map(tmp_path) -> Path:
    p = tmp_path / "yamnet_class_map.csv"
    p.write_text(
        "index,mid,display_name\n"
        "0,/m/09x0r,Speech\n"
        "69,/m/0bt9lr,Dog\n"
        "70,/m/05tny_,Bark\n"
        "300,/m/hotdog,Hot dog\n"
    )
    return p


class FakeInterpreter:
    def __init__(self, scores: np.ndarray):
        self.scores = scores
        self.input = None

    def get_input_details(self):
        return [{"index": 0}]

    def get_output_details(self):
        return [{"index": 1}]

    def resize_tensor_input(self, index, shape):
        pass

    def allocate_tensors(self):
        pass

    def set_tensor(self, index, data):
        self.input = data

    def invoke(self):
        pass

    def get_tensor(self, index):
        return self.scores


def test_dog_indices_exact_match(class_map):
    assert load_dog_indices(class_map) == {69: "Dog", 70: "Bark"}


def test_dog_indices_missing_raises(tmp_path):
    p = tmp_path / "map.csv"
    p.write_text("index,mid,display_name\n0,/m/x,Speech\n")
    with pytest.raises(ValueError):
        load_dog_indices(p)


def test_score_is_max_over_dog_classes(class_map):
    patches = np.zeros((2, 521), dtype=np.float32)
    patches[:, 0] = 0.99  # Speech is ignored
    patches[:, 70] = [0.7, 0.9]  # Bark mean 0.8
    patches[:, 69] = [0.3, 0.3]  # Dog mean 0.3
    fake = FakeInterpreter(patches)
    det = BarkDetector(Path("unused.tflite"), class_map, interpreter_factory=lambda _: fake)
    assert det.score(np.zeros(15360, dtype=np.float32)) == pytest.approx(0.8)
    assert fake.input is not None and fake.input.dtype == np.float32


def test_to_16k_length():
    out = to_16k(np.zeros(48000, dtype=np.float32), 48000)
    assert out.shape == (16000,)
    assert out.dtype == np.float32
    same = np.ones(16000, dtype=np.float32)
    assert to_16k(same, 16000) is same


def test_loudness():
    t = np.arange(48000) / 48000
    sine = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    assert loudness_dbfs(sine) == pytest.approx(-3.01, abs=0.1)
    assert loudness_dbfs(np.zeros(1000, dtype=np.float32)) == -120.0


MODEL = Path(os.environ.get("CHASE_MODEL", "/models/yamnet.tflite"))


@pytest.mark.integration
@pytest.mark.skipif(not MODEL.exists(), reason="YAMNet model not present")
def test_real_model_scores_silence_low():
    det = BarkDetector(MODEL, MODEL.with_name("yamnet_class_map.csv"))
    assert det.score(np.zeros(16000, dtype=np.float32)) < 0.2

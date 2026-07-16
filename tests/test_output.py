from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from serum_render.output import write_audio


def _audio():
    # (channels, samples) float32, as the engine returns it
    return np.zeros((2, 100), dtype=np.float32)


def test_wav_written_and_transposed(tmp_path: Path):
    out = tmp_path / "a.wav"
    write_audio(_audio(), str(out), 44100, "16", "wav")
    data, sr = sf.read(str(out))
    assert sr == 44100
    assert data.shape == (100, 2)  # (samples, channels) on disk


@pytest.mark.parametrize("depth,subtype", [("16", "PCM_16"), ("24", "PCM_24"), ("32f", "FLOAT")])
def test_bit_depths(tmp_path: Path, depth, subtype):
    out = tmp_path / f"a_{depth}.wav"
    write_audio(_audio(), str(out), 44100, depth, "wav")
    info = sf.info(str(out))
    assert info.subtype == subtype


def test_npy_skips_bit_depth(tmp_path: Path):
    out = tmp_path / "a.npy"
    write_audio(_audio(), str(out), 44100, "ignored", "npy")
    arr = np.load(str(out))
    assert arr.shape == (2, 100)  # raw engine layout, no transpose


def test_creates_parent_dirs(tmp_path: Path):
    out = tmp_path / "deep" / "nested" / "a.wav"
    write_audio(_audio(), str(out), 44100, "16", "wav")
    assert out.exists()


def test_unknown_bit_depth_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="bit depth"):
        write_audio(_audio(), str(tmp_path / "a.wav"), 44100, "8", "wav")


def test_unknown_format_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="format"):
        write_audio(_audio(), str(tmp_path / "a.flac"), 44100, "16", "flac")

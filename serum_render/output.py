"""Audio file writing. Part of the worker import graph — module level is
stdlib-only; numpy/soundfile load inside the function, after dawdreamer
has already been imported by EngineHost."""
from __future__ import annotations

from pathlib import Path

_SUBTYPE_MAP = {"16": "PCM_16", "24": "PCM_24", "32f": "FLOAT"}


def write_audio(
    audio, output_path: str, sample_rate: int, bit_depth: str, output_format: str
) -> None:
    """Write a (channels, samples) float32 array as wav (given bit depth)
    or npy (raw float32, bit depth ignored)."""
    import numpy as np

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if output_format == "npy":
        np.save(output_path, audio)
        return
    if output_format != "wav":
        raise ValueError(f"Unknown output format: {output_format!r}")

    subtype = _SUBTYPE_MAP.get(bit_depth)
    if subtype is None:
        raise ValueError(f"Unknown bit depth: {bit_depth!r}")

    import soundfile as sf

    # soundfile expects (samples, channels) — transpose before writing.
    sf.write(output_path, audio.T, sample_rate, subtype=subtype)

"""Audio file writing. Part of the worker import graph — module level is
stdlib-only; numpy/soundfile load inside the function, after dawdreamer
has already been imported by EngineHost."""
from __future__ import annotations

import os
from pathlib import Path

_SUBTYPE_MAP = {"16": "PCM_16", "24": "PCM_24", "32f": "FLOAT"}


def write_audio(
    audio, output_path: str, sample_rate: int, bit_depth: str, output_format: str
) -> None:
    """Write a (channels, samples) float32 array as wav (given bit depth)
    or npy (raw float32, bit depth ignored).

    Writes to a sibling temp file and renames it into place, so a killed
    render can never leave a truncated file at `output_path` —
    skip_existing would otherwise skip that garbage forever on the
    re-run. A SIGKILL does leave a stray `.tmp` behind; that is the
    intended trade.

    The temp suffix goes *after* the real extension (`a.wav.tmp`, not
    `a.tmp.wav`) so strays stay out of `*.wav` globs. Both writers are
    told their format explicitly, because each otherwise infers it from
    the trailing extension: np.save would write `a.npy.tmp.npy`, and
    sf.write raises on an unrecognised one.
    """
    import numpy as np

    if output_format not in ("wav", "npy"):
        raise ValueError(f"Unknown output format: {output_format!r}")
    subtype = None
    if output_format == "wav":
        subtype = _SUBTYPE_MAP.get(bit_depth)
        if subtype is None:
            raise ValueError(f"Unknown bit depth: {bit_depth!r}")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{output_path}.tmp"
    try:
        if output_format == "npy":
            with open(tmp, "wb") as f:
                np.save(f, audio)
        else:
            import soundfile as sf

            # soundfile expects (samples, channels) — transpose before writing.
            sf.write(tmp, audio.T, sample_rate, subtype=subtype, format="WAV")
        os.replace(tmp, output_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

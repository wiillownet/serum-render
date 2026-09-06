"""Audio file writing. Part of the worker import graph — module level is
stdlib-only; numpy/soundfile load inside the function, after dawdreamer
has already been imported by EngineHost."""
from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple


class Format(NamedTuple):
    extension: str
    sf_format: str | None  # soundfile/libsndfile container; None = numpy
    # bit depth -> libsndfile subtype; None = bit depth ignored
    subtypes: dict[str, str] | None


# libsndfile's compression_level for Vorbis: 0.0 = largest/best, 1.0 =
# smallest. 0.6 is libsndfile's own default (Vorbis quality 0.4, ~128 kbps
# stereo). Named here so a --ogg-quality flag can expose it later.
OGG_COMPRESSION_LEVEL = 0.6

# The one table every output-format decision reads: --format validation,
# extension derivation, and the writer. libsndfile 1.2.2 covers all three
# audio containers; no extra dependency.
FORMATS: dict[str, Format] = {
    "wav": Format(".wav", "WAV", {"16": "PCM_16", "24": "PCM_24", "32f": "FLOAT"}),
    "flac": Format(".flac", "FLAC", {"16": "PCM_16", "24": "PCM_24"}),
    # Vorbis at libsndfile's default quality; lossy, so bit depth is moot.
    "ogg": Format(".ogg", "OGG", None),
    "npy": Format(".npy", None, None),
}


def check_bit_depth(output_format: str, bit_depth: str) -> None:
    """Raise ValueError if `bit_depth` is not writable in `output_format`."""
    fmt = FORMATS[output_format]
    if fmt.subtypes is not None and bit_depth not in fmt.subtypes:
        raise ValueError(
            f"{output_format} cannot be written at bit depth {bit_depth!r}; "
            f"choose one of {', '.join(fmt.subtypes)}"
        )


def write_audio(
    audio, output_path: str, sample_rate: int, bit_depth: str, output_format: str
) -> None:
    """Write a (channels, samples) float32 array in `output_format` (see
    FORMATS): wav/flac at the given bit depth, ogg (Vorbis) and npy (raw
    float32) with bit depth ignored.

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

    if output_format not in FORMATS:
        raise ValueError(f"Unknown output format: {output_format!r}")
    check_bit_depth(output_format, bit_depth)
    fmt = FORMATS[output_format]

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{output_path}.tmp"
    try:
        if fmt.sf_format is None:
            with open(tmp, "wb") as f:
                np.save(f, audio)
        else:
            import soundfile as sf

            subtype = fmt.subtypes[bit_depth] if fmt.subtypes else None
            extra = {"compression_level": OGG_COMPRESSION_LEVEL} if output_format == "ogg" else {}
            # soundfile expects (samples, channels) — transpose before writing.
            sf.write(tmp, audio.T, sample_rate, subtype=subtype, format=fmt.sf_format, **extra)
        os.replace(tmp, output_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

"""Frozen render configuration. Stdlib-only at module level."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .formats import PresetFormat

# Peak below this is treated as silent output by the engine.
# -90 dBFS ~= 16-bit quantization floor. Advisory only — known to be
# conservative for 24/32f bit depths.
SILENCE_EPS = 3.16e-5


# Standard Serum install locations, used as fallbacks when no explicit
# plugin path is given. INVARIANT: the SERUM1 entry must be a VST2 binary —
# the Serum 1 VST3 silently mis-loads .fxp presets. On Windows the 64-bit
# VST2 really does live in the VST3 folder (Xfer installer quirk).
_DEFAULT_PLUGIN_PATHS: dict[str, dict[PresetFormat, str]] = {
    "darwin": {
        PresetFormat.SERUM1: "/Library/Audio/Plug-Ins/VST/Serum.vst",
        PresetFormat.SERUM2: "/Library/Audio/Plug-Ins/VST3/Serum2.vst3",
    },
    "win32": {
        PresetFormat.SERUM1: "C:/Program Files/Common Files/VST3/Serum_x64.dll",
        PresetFormat.SERUM2: "C:/Program Files/Common Files/VST3/Serum2.vst3",
    },
}


def default_plugin_path(fmt: PresetFormat, platform: str | None = None) -> Path | None:
    """Return the standard install path for a format if it exists on disk.

    A missing default is "unset" (returns None), never an error — the
    caller falls through to its normal missing-plugin message.
    """
    platform = platform if platform is not None else sys.platform
    table = _DEFAULT_PLUGIN_PATHS.get(platform)
    if table is None:
        return None
    candidate = Path(table[fmt])
    return candidate if candidate.exists() else None


@dataclass(frozen=True)
class RenderConfig:
    # At least one of these must be set. `serum1_plugin_path` accepts either
    # the VST2 binary or the VST3 build of Serum 1 for library users who
    # know what they're doing — but only the VST2 build loads .fxp
    # correctly, so the CLI default never picks the VST3.
    # `serum2_plugin_path` is Serum 2's VST3, paired with `load_state`.
    serum1_plugin_path: str | Path | None = None
    serum2_plugin_path: str | Path | None = None
    sample_rate: int = 44100
    note: int = 48
    velocity: int = 127
    duration: float = 1.0
    tail: float = 1.0
    midi_path: str | Path | None = None
    # Render every preset in a fresh single-use process, making batch
    # output bit-reproducible. Costs a plugin load per preset instead of
    # per worker. In-process resets don't work for Serum 1 (state
    # survives even a full engine reload); see docs/decisions.md.
    deterministic: bool = False

    def __post_init__(self) -> None:
        # Cheap shape/range checks only — no disk I/O. Path existence is
        # verified on first use (renderer entry), keeping construction free
        # of filesystem side effects.
        if self.serum1_plugin_path is None and self.serum2_plugin_path is None:
            raise ValueError(
                "RenderConfig requires at least one of serum1_plugin_path or "
                "serum2_plugin_path to be set."
            )
        for field in ("serum1_plugin_path", "serum2_plugin_path", "midi_path"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, Path(value))

        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0, got {self.sample_rate}")
        if not (0 <= self.note <= 127):
            raise ValueError(f"note must be 0-127, got {self.note}")
        if not (1 <= self.velocity <= 127):
            raise ValueError(f"velocity must be 1-127, got {self.velocity}")
        if self.duration <= 0:
            raise ValueError(f"duration must be > 0, got {self.duration}")
        if self.tail < 0:
            raise ValueError(f"tail must be >= 0, got {self.tail}")

    def plugin_path_for(self, fmt: PresetFormat) -> Path | None:
        return {
            PresetFormat.SERUM1: self.serum1_plugin_path,
            PresetFormat.SERUM2: self.serum2_plugin_path,
        }[fmt]

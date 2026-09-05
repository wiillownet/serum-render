"""Frozen render configuration. Stdlib-only at module level."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .formats import PresetFormat, suffix_for

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


# Expected binary suffix per platform per format, derived from the same
# knowledge as _DEFAULT_PLUGIN_PATHS above so a check and a default can never
# disagree. Serum 1 is VST2 (a different extension on every platform); Serum 2
# is VST3 everywhere. INVARIANT: never `.vst3` for SERUM1 — the Serum 1 VST3
# silently mis-loads .fxp presets, which is the failure this table exists to
# catch when a user browses by hand.
_PLUGIN_SUFFIXES: dict[str, dict[PresetFormat, str]] = {
    "darwin": {PresetFormat.SERUM1: ".vst", PresetFormat.SERUM2: ".vst3"},
    "win32": {PresetFormat.SERUM1: ".dll", PresetFormat.SERUM2: ".vst3"},
    "linux": {PresetFormat.SERUM1: ".so", PresetFormat.SERUM2: ".vst3"},
}


def plugin_suffix_for(fmt: PresetFormat, platform: str | None = None) -> str | None:
    """Expected binary suffix for a format on a platform, or None if the
    platform is unknown (in which case callers should not enforce a check)."""
    platform = platform if platform is not None else sys.platform
    table = _PLUGIN_SUFFIXES.get(platform)
    return None if table is None else table[fmt]


def plugin_path_looks_valid(
    path: str | Path, fmt: PresetFormat, platform: str | None = None
) -> bool:
    """True when `path` exists and carries the suffix this format expects.

    Existence is `exists()`, not `is_file()`: on macOS both plugin formats are
    bundles — directories — so an is_file() check rejects every valid macOS
    plugin. An unknown platform has no expectation to violate, so only
    existence is checked there.
    """
    path = Path(path)
    if not path.exists():
        return False
    suffix = plugin_suffix_for(fmt, platform)
    return suffix is None or path.suffix.lower() == suffix


# Where Serum keeps its factory presets when they have not been moved.
# The macOS entries are verified against a real install (2026-09-01). The
# Windows entries are NOT verified on a Windows machine — that is safe
# here, because `default_preset_dir` only ever returns a directory that
# exists and actually holds presets of that format, so a wrong guess
# degrades to "not found" instead of to a wrong answer.
_DEFAULT_PRESET_DIRS: dict[str, dict[PresetFormat, str]] = {
    "darwin": {
        PresetFormat.SERUM1: "/Library/Audio/Presets/Xfer Records/Serum Presets",
        PresetFormat.SERUM2: "/Library/Audio/Presets/Xfer Records/Serum 2 Presets",
    },
    "win32": {
        PresetFormat.SERUM1: "C:/ProgramData/Xfer/Serum Presets",
        PresetFormat.SERUM2: "C:/ProgramData/Xfer/Serum 2 Presets",
    },
}

# Serum records its own preset folder in these prefs files. Reading it
# beats assuming the default, because the folder is relocatable from
# inside the plugin. A blank or whitespace-only value means "unset" —
# that is what Serum 1 writes when the folder has never been moved.
# Windows paths unverified, same reasoning as above.
_PREFS_PATHS: dict[str, dict[PresetFormat, str]] = {
    "darwin": {
        PresetFormat.SERUM1: "~/Library/Preferences/SerumPrefs.json",
        PresetFormat.SERUM2: "~/Library/Preferences/Serum2Prefs.json",
    },
    "win32": {
        PresetFormat.SERUM1: "~/AppData/Roaming/Xfer/Serum/SerumPrefs.json",
        PresetFormat.SERUM2: "~/AppData/Roaming/Xfer/Serum2/Serum2Prefs.json",
    },
}
_PREFS_PRESET_KEY = "Serum Presets Path"


def _configured_preset_dir(fmt: PresetFormat, platform: str) -> Path | None:
    """The preset folder Serum itself records, or None if it says nothing.

    Never raises: an absent, unreadable or malformed prefs file is just
    "no opinion", and the caller falls through to the standard location.
    """
    table = _PREFS_PATHS.get(platform)
    if table is None:
        return None
    try:
        data = json.loads(Path(table[fmt]).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get(_PREFS_PRESET_KEY) if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value.strip())


def _holds_preset(directory: Path, suffix: str) -> bool:
    """True as soon as one preset of that suffix turns up. Stops at the
    first hit — the factory tree runs to thousands of files."""
    try:
        for path in directory.rglob("*"):
            if path.suffix.lower() == suffix:
                return True
    except OSError:
        return False
    return False


def default_preset_dir(
    fmt: PresetFormat, platform: str | None = None
) -> Path | None:
    """Best guess at where this format's presets live, or None.

    Prefers the folder Serum records in its own prefs over the standard
    install location. Only returns a directory that exists AND contains
    at least one preset of that format, so a stale prefs entry or a
    wrong default reads as "not found" rather than as an empty batch.
    """
    platform = platform if platform is not None else sys.platform
    candidates: list[Path] = []
    configured = _configured_preset_dir(fmt, platform)
    if configured is not None:
        candidates.append(configured)
    table = _DEFAULT_PRESET_DIRS.get(platform)
    if table is not None:
        candidates.append(Path(table[fmt]))

    suffix = suffix_for(fmt).lower()
    for candidate in candidates:
        if candidate.is_dir() and _holds_preset(candidate, suffix):
            return candidate
    return None


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

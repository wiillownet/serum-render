"""Preset formats. Stdlib-only at module level (worker import graph)."""
from __future__ import annotations

from enum import Enum
from pathlib import Path


class PresetFormat(str, Enum):
    """Preset file format. Drives engine dispatch.

    String-valued so the format field stays legible when a Job crosses
    the loky process boundary via cloudpickle.
    """
    SERUM1 = "serum1"
    SERUM2 = "serum2"


# Suffix -> format. Serum 1 + Serum 2 only, permanently (docs/decisions.md).
_SUFFIX_TO_FORMAT: dict[str, PresetFormat] = {
    ".fxp": PresetFormat.SERUM1,
    ".SerumPreset": PresetFormat.SERUM2,
}
# Repacked and hand-renamed presets show up with uppercase suffixes, and
# the directory scan's rglob already matches them case-insensitively on
# Windows. Match on a folded key; keep the canonical spellings for errors.
_FOLDED_SUFFIX_TO_FORMAT: dict[str, PresetFormat] = {
    suffix.lower(): fmt for suffix, fmt in _SUFFIX_TO_FORMAT.items()
}


def format_or_none(path: Path) -> PresetFormat | None:
    """Resolve a path to its PresetFormat, or None if the suffix is not a
    supported one. The single place suffix matching happens, so directory
    discovery and single-path dispatch can never disagree.

    Matching folds case. A stemless name like `.fxp` has an empty suffix
    under pathlib's dotfile rule, so the whole name is the fallback key —
    presets really do get saved with an empty filename.
    """
    return _FOLDED_SUFFIX_TO_FORMAT.get(path.suffix.lower() or path.name.lower())


def format_for_path(path: Path) -> PresetFormat:
    """Return the PresetFormat for a path's suffix.

    Raises ValueError on an unknown suffix so callers (single-file CLI
    mode) get a clean error rather than a silent dispatch surprise.
    """
    fmt = format_or_none(path)
    if fmt is None:
        supported = ", ".join(sorted(_SUFFIX_TO_FORMAT))
        raise ValueError(
            f"Unsupported preset suffix {path.suffix!r} on {path.name}; "
            f"supported: {supported}"
        )
    return fmt

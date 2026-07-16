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


def format_for_path(path: Path) -> PresetFormat:
    """Return the PresetFormat for a path's suffix.

    Raises ValueError on an unknown suffix so callers (single-file CLI
    mode) get a clean error rather than a silent dispatch surprise.
    """
    fmt = _SUFFIX_TO_FORMAT.get(path.suffix)
    if fmt is None:
        supported = ", ".join(sorted(_SUFFIX_TO_FORMAT))
        raise ValueError(
            f"Unsupported preset suffix {path.suffix!r} on {path.name}; "
            f"supported: {supported}"
        )
    return fmt

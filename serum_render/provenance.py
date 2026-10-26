"""
Render provenance: which serum-render, serum2-preset-loader and plugin
versions made a file, and which Serum version the preset was saved with.

One dict feeds both the --json `start` event and the per-file lines of
PROVENANCE_FILE in the output folder. Module level is stdlib-only, like
engine.py; the loader is imported inside the functions that need it.
"""
from __future__ import annotations

import json
import plistlib
import struct
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .formats import PresetFormat

PROVENANCE_FILE = "serum-render-provenance.jsonl"


def _dist_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def plugin_version(plugin_path: str | Path) -> str | None:
    """CFBundleShortVersionString from a macOS plugin bundle's Info.plist.
    None elsewhere: Windows has no source we trust yet."""
    if sys.platform != "darwin":
        return None
    plist = Path(plugin_path) / "Contents" / "Info.plist"
    try:
        with plist.open("rb") as f:
            value = plistlib.load(f).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException):
        return None
    return value if isinstance(value, str) else None


def run_provenance(plugin_paths: dict[PresetFormat, Path]) -> dict:
    """Run-level provenance, read once in the parent process."""
    return {
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "serum_render": _dist_version("serum-render"),
        "serum2_preset_loader": _dist_version("serum2-preset-loader"),
        "plugins": {
            fmt.value: plugin_version(path) for fmt, path in plugin_paths.items()
        },
    }


def _state_metadata(state: bytes) -> dict:
    """The XferJson header of the IComponent inside a converted state blob.

    Its productVersion is the one the converter copied from the preset body.
    Only the header's bytes are decoded: the whole IComponent of a
    sample-based preset is hundreds of MB, and JUCE base64 decodes a prefix
    exactly when the prefix is whole 3-byte groups (4 chars each), or the
    whole block.
    """
    from serum2_preset_loader import juce_memoryblock_b64decode

    tag = b"<IComponent>"
    start = state.find(tag)
    dot = state.find(b".", start) if start >= 0 else -1
    if dot < 0:
        raise ValueError(
            "converted state has no <IComponent>N.… block; the "
            "serum2-preset-loader state layout changed"
        )
    start += len(tag)
    total = int(state[start:dot])
    start = dot + 1

    def prefix(n: int) -> bytes:
        n = min(-(-n // 3) * 3, total)
        chars = state[start:start + (n * 8 + 5) // 6].decode("ascii")
        return juce_memoryblock_b64decode(f"{n}.{chars}")

    head = prefix(17)
    if head[:9] != b"XferJson\x00":
        raise ValueError(f"state IComponent is not XferJson: {head[:9]!r}")
    json_len = struct.unpack("<Q", head[9:17])[0]
    return json.loads(prefix(17 + json_len)[17:17 + json_len])


def preset_versions(state: bytes, header: dict) -> dict:
    """Per-render fields for a Serum 2 preset. ``preset_version`` is the
    body's productVersion as the converter applied it (a body with none gets
    the converter's fallback). When the file header says otherwise, both
    are kept and the render is flagged."""
    body = _state_metadata(state).get("productVersion")
    out = {"preset_version": body}
    head = header.get("productVersion")
    if head != body:
        out["preset_header_version"] = head
        out["preset_version_mismatch"] = True
    return out

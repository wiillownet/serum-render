"""Preset discovery, filename composition, output-path assignment, MIDI
duration. Main-process only — never imported inside a worker."""
from __future__ import annotations

import re
from pathlib import Path

import mido

from .formats import PresetFormat, _SUFFIX_TO_FORMAT, format_for_path

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")
_UNDERSCORE_RUN_RE = re.compile(r"_+")

# Stem truncation cap: leaves 4 chars of headroom for collision suffixes
# (_1 through _999) so the total filename stays under the Windows 200-char
# comfort zone. Bounds the FILENAME, not the full PATH — deeply nested
# output dirs can still hit the 260-char MAX_PATH limit.
_STEM_MAX_LEN = 196


def discover_presets(
    path: Path, recurse: bool = True
) -> list[tuple[Path, PresetFormat]]:
    """
    Discover preset files at the given path.

    - Single file: format inferred from suffix; unknown suffix raises
      ValueError (a misnamed file would dispatch to the wrong engine).
    - Directory: globs every supported extension, recursively by default,
      sorted alphabetically by absolute path.
    - Raises FileNotFoundError if `path` does not exist.

    Returns a list of `(absolute_path, format)` tuples.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Preset path not found: {path}")

    if path.is_file():
        return [(path.resolve(), format_for_path(path))]

    files: list[tuple[Path, PresetFormat]] = []
    for suffix, fmt in _SUFFIX_TO_FORMAT.items():
        pattern = f"*{suffix}"
        matches = path.rglob(pattern) if recurse else path.glob(pattern)
        for f in matches:
            files.append((f.resolve(), fmt))

    return sorted(files, key=lambda t: t[0])


def sanitize(value: str) -> str:
    """
    Sanitize a single template-variable value for use in a filename.
    Keeps [A-Za-z0-9_-]; everything else (spaces, brackets, punctuation,
    unicode) becomes '_'. Runs of underscores collapse to one, and leading
    and trailing underscores are stripped.
    """
    value = value.strip()
    value = _SANITIZE_RE.sub("_", value)
    value = _UNDERSCORE_RUN_RE.sub("_", value)
    return value.strip("_")


def compose_filename(
    template: str,
    preset_path: Path,
    presets_root: Path | None,
    note: int,
    velocity: int,
) -> str:
    """
    Compose a filename stem by substituting template variables against a
    preset path. Returns the stem only (no extension), truncated to 196
    chars. Collision suffixes are applied later by resolve_output_paths().

    presets_root must be absolute (matching discover_presets output) or
    None in single-file mode; {subpath} resolves to "" and any adjacent
    separator is collapsed.
    """
    preset = sanitize(preset_path.stem)
    folder = sanitize(preset_path.parent.name)

    if presets_root is not None:
        try:
            rel = preset_path.parent.relative_to(presets_root)
        except ValueError:
            subpath = ""
        else:
            subpath = sanitize("_".join(rel.parts)) if rel.parts else ""
    else:
        subpath = ""

    result = template
    result = result.replace("{preset}", preset)
    result = result.replace("{note}", str(note))
    result = result.replace("{velocity}", str(velocity))
    result = result.replace("{folder}", folder)
    result = result.replace("{subpath}", subpath)

    # Collapse separators that an empty {subpath} would leave behind
    # (e.g. "{subpath}_{preset}" -> "_{preset}" -> "preset").
    result = _UNDERSCORE_RUN_RE.sub("_", result).strip("_")

    return result[:_STEM_MAX_LEN]


def resolve_output_paths(
    stems: list[str],
    output_dir: Path,
    extension: str,
) -> list[str]:
    """
    Turn filename stems into unique absolute output path strings, in input
    order. Collisions are disambiguated with `_1`, `_2`, ...; the 196-char
    stem cap leaves room for suffixes up to `_999` without re-truncation.

    An empty stem (a preset name that sanitizes to nothing) falls back to
    a zero-padded `preset_NNNN` so the job gets a stable output name.
    """
    seen: dict[str, int] = {}
    paths: list[str] = []
    for idx, stem in enumerate(stems):
        stem = stem or f"preset_{idx:04d}"
        if stem not in seen:
            seen[stem] = 0
            final_stem = stem
        else:
            seen[stem] += 1
            final_stem = f"{stem}_{seen[stem]}"
        paths.append(str(output_dir / f"{final_stem}{extension}"))
    return paths


def get_midi_duration(midi_path: Path) -> float:
    """
    Return the total playback duration of a MIDI file in seconds.

    Raises TypeError for Type 2 (asynchronous) files, which cannot have a
    linear duration. Raises ValueError if mido cannot parse the file.
    """
    try:
        mid = mido.MidiFile(str(midi_path))
    except Exception as exc:
        raise ValueError(f"Could not parse MIDI file '{midi_path}': {exc}") from exc

    if mid.type == 2:
        raise TypeError(
            f"MIDI file '{midi_path}' is Type 2 (asynchronous) and duration "
            f"cannot be determined. Convert it to Type 0 or Type 1 before "
            f"rendering."
        )

    return mid.length

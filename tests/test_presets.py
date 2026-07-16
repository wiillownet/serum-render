from pathlib import Path

import pytest

from serum_render.discover import discover_presets
from serum_render.formats import PresetFormat, format_for_path


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")


# ─── format_for_path ──────────────────────────────────────────────────

def test_format_for_path_fxp():
    assert format_for_path(Path("foo.fxp")) is PresetFormat.SERUM1


def test_format_for_path_serum2():
    assert format_for_path(Path("foo.SerumPreset")) is PresetFormat.SERUM2


def test_format_for_path_unknown_raises():
    with pytest.raises(ValueError, match="Unsupported preset suffix"):
        format_for_path(Path("foo.bogus"))


def test_format_for_path_no_suffix_raises():
    with pytest.raises(ValueError, match="Unsupported preset suffix"):
        format_for_path(Path("README"))


# ─── discover_presets — single file mode ──────────────────────────────

def test_single_fxp_file_returns_format_tagged(tmp_path: Path):
    f = tmp_path / "one.fxp"
    _touch(f)
    assert discover_presets(f) == [(f.resolve(), PresetFormat.SERUM1)]


def test_single_serumpreset_file_returns_format_tagged(tmp_path: Path):
    f = tmp_path / "one.SerumPreset"
    _touch(f)
    assert discover_presets(f) == [(f.resolve(), PresetFormat.SERUM2)]


def test_single_file_unknown_suffix_raises(tmp_path: Path):
    """Tightened contract: single-file mode no longer trusts the caller."""
    f = tmp_path / "one.txt"
    _touch(f)
    with pytest.raises(ValueError, match="Unsupported preset suffix"):
        discover_presets(f)


# ─── discover_presets — directory mode ────────────────────────────────

def test_directory_recursive_by_default(tmp_path: Path):
    _touch(tmp_path / "a.fxp")
    _touch(tmp_path / "sub" / "b.fxp")
    names = [p.name for p, _fmt in discover_presets(tmp_path)]
    assert "a.fxp" in names and "b.fxp" in names


def test_directory_no_recurse(tmp_path: Path):
    _touch(tmp_path / "a.fxp")
    _touch(tmp_path / "sub" / "b.fxp")
    names = [p.name for p, _fmt in discover_presets(tmp_path, recurse=False)]
    assert names == ["a.fxp"]


def test_directory_finds_both_formats(tmp_path: Path):
    _touch(tmp_path / "a.fxp")
    _touch(tmp_path / "b.SerumPreset")
    found = discover_presets(tmp_path)
    by_name = {p.name: fmt for p, fmt in found}
    assert by_name == {
        "a.fxp": PresetFormat.SERUM1,
        "b.SerumPreset": PresetFormat.SERUM2,
    }


def test_results_sorted_alphabetically_across_formats(tmp_path: Path):
    """Mixed-format results are sorted by absolute path, not by format."""
    for n in ["c.fxp", "a.SerumPreset", "b.fxp"]:
        _touch(tmp_path / n)
    names = [p.name for p, _fmt in discover_presets(tmp_path)]
    assert names == ["a.SerumPreset", "b.fxp", "c.fxp"]


def test_nonexistent_path_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        discover_presets(tmp_path / "does_not_exist")


def test_empty_directory_returns_empty(tmp_path: Path):
    assert discover_presets(tmp_path) == []


def test_unrelated_files_ignored(tmp_path: Path):
    """Only .fxp and .SerumPreset suffixes should match."""
    _touch(tmp_path / "a.fxp")
    _touch(tmp_path / "b.wav")
    _touch(tmp_path / "c.txt")
    _touch(tmp_path / "d.SerumPreset")
    found = sorted(p.name for p, _fmt in discover_presets(tmp_path))
    assert found == ["a.fxp", "d.SerumPreset"]


def test_results_are_absolute(tmp_path: Path):
    _touch(tmp_path / "a.fxp")
    _touch(tmp_path / "b.SerumPreset")
    for p, _fmt in discover_presets(tmp_path):
        assert p.is_absolute()

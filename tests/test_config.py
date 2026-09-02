import dataclasses
import json
from pathlib import Path

import pytest

import serum_render.config as config
from serum_render.config import RenderConfig, default_plugin_path
from serum_render.formats import PresetFormat, format_for_path, suffix_for


def test_defaults_resolve():
    cfg = RenderConfig(serum1_plugin_path="C:/fake/Serum_x64.dll")
    assert cfg.sample_rate == 44100
    assert cfg.note == 48
    assert cfg.velocity == 127
    assert cfg.duration == 1.0
    assert cfg.tail == 1.0
    assert cfg.midi_path is None
    assert cfg.serum2_plugin_path is None


def test_serum1_plugin_path_coerced_to_path():
    cfg = RenderConfig(serum1_plugin_path="C:/fake/Serum_x64.dll")
    assert isinstance(cfg.serum1_plugin_path, Path)


def test_serum2_plugin_path_coerced_to_path():
    cfg = RenderConfig(serum2_plugin_path="C:/fake/Serum2.vst3")
    assert isinstance(cfg.serum2_plugin_path, Path)
    assert cfg.serum1_plugin_path is None


def test_both_plugin_paths_set_is_allowed():
    cfg = RenderConfig(
        serum1_plugin_path="C:/fake/Serum_x64.dll",
        serum2_plugin_path="C:/fake/Serum2.vst3",
    )
    assert isinstance(cfg.serum1_plugin_path, Path)
    assert isinstance(cfg.serum2_plugin_path, Path)


def test_no_plugin_path_rejected():
    with pytest.raises(ValueError, match="at least one"):
        RenderConfig()


def test_config_is_frozen():
    cfg = RenderConfig(serum1_plugin_path="p")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.note = 60


def test_construction_does_no_existence_check():
    # Paths don't need to exist at construction — existence is a
    # first-use check at the renderer entry.
    RenderConfig(serum1_plugin_path="/definitely/not/real.vst")


def test_plugin_path_for():
    cfg = RenderConfig(
        serum1_plugin_path="a.vst", serum2_plugin_path="b.vst3"
    )
    assert cfg.plugin_path_for(PresetFormat.SERUM1) == Path("a.vst")
    assert cfg.plugin_path_for(PresetFormat.SERUM2) == Path("b.vst3")


def test_midi_path_coerced_to_path_when_set():
    cfg = RenderConfig(serum1_plugin_path="p", midi_path="seq.mid")
    assert isinstance(cfg.midi_path, Path)


def test_midi_path_stays_none_when_not_set():
    cfg = RenderConfig(serum1_plugin_path="p")
    assert cfg.midi_path is None


@pytest.mark.parametrize("sr", [0, -1, -44100])
def test_sample_rate_must_be_positive(sr):
    with pytest.raises(ValueError, match="sample_rate"):
        RenderConfig(serum1_plugin_path="p", sample_rate=sr)


@pytest.mark.parametrize("note", [-1, 128, 999])
def test_note_range_enforced(note):
    with pytest.raises(ValueError, match="note"):
        RenderConfig(serum1_plugin_path="p", note=note)


def test_note_at_boundaries_allowed():
    RenderConfig(serum1_plugin_path="p", note=0)
    RenderConfig(serum1_plugin_path="p", note=127)


@pytest.mark.parametrize("vel", [0, -1, 128])
def test_velocity_range_enforced(vel):
    with pytest.raises(ValueError, match="velocity"):
        RenderConfig(serum1_plugin_path="p", velocity=vel)


def test_velocity_boundaries():
    RenderConfig(serum1_plugin_path="p", velocity=1)
    RenderConfig(serum1_plugin_path="p", velocity=127)


@pytest.mark.parametrize("dur", [0, -0.1, -1.0])
def test_duration_must_be_positive(dur):
    with pytest.raises(ValueError, match="duration"):
        RenderConfig(serum1_plugin_path="p", duration=dur)


@pytest.mark.parametrize("tail", [-0.1, -1.0])
def test_tail_must_be_non_negative(tail):
    with pytest.raises(ValueError, match="tail"):
        RenderConfig(serum1_plugin_path="p", tail=tail)


def test_tail_zero_accepted():
    # tail=0 is legitimate for percussive one-shots that don't need a
    # release-envelope tail.
    RenderConfig(serum1_plugin_path="p", tail=0)


# --- default plugin path resolution ------------------------------------

def _patch_default(monkeypatch, platform, fmt, exists):
    """Point the default table at a tmp path that does or doesn't exist."""
    import serum_render.config as config_mod

    monkeypatch.setattr(
        config_mod.Path, "exists", lambda self: exists
    )


def test_default_path_returned_when_exists(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: True)
    p = default_plugin_path(PresetFormat.SERUM1, platform="darwin")
    assert p == Path("/Library/Audio/Plug-Ins/VST/Serum.vst")


def test_default_path_none_when_missing(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert default_plugin_path(PresetFormat.SERUM1, platform="darwin") is None


def test_default_serum1_is_vst2_on_both_platforms(monkeypatch):
    # INVARIANT: the SERUM1 default must be a VST2 binary — the Serum 1
    # VST3 silently mis-loads .fxp presets. On macOS that's the .vst
    # bundle; on Windows it's Serum_x64.dll (which lives in the VST3
    # folder — Xfer installer quirk, not a mistake here).
    monkeypatch.setattr(Path, "exists", lambda self: True)
    mac = default_plugin_path(PresetFormat.SERUM1, platform="darwin")
    win = default_plugin_path(PresetFormat.SERUM1, platform="win32")
    assert mac.suffix == ".vst"
    assert win.name == "Serum_x64.dll"


def test_default_serum2_paths(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: True)
    mac = default_plugin_path(PresetFormat.SERUM2, platform="darwin")
    win = default_plugin_path(PresetFormat.SERUM2, platform="win32")
    assert mac == Path("/Library/Audio/Plug-Ins/VST3/Serum2.vst3")
    assert win == Path("C:/Program Files/Common Files/VST3/Serum2.vst3")


def test_default_path_none_on_unknown_platform():
    assert default_plugin_path(PresetFormat.SERUM1, platform="linux") is None


# ---- preset directory detection -------------------------------------------


def _preset_tree(root: Path, suffix: str, name: str = "Bass") -> Path:
    """A directory that actually holds a preset of that format."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "Sub").mkdir(exist_ok=True)
    (root / "Sub" / f"{name}{suffix}").write_bytes(b"")
    return root


@pytest.fixture
def preset_env(tmp_path, monkeypatch):
    """Point both lookup tables at tmp_path for a fake 'testos' platform."""
    prefs = tmp_path / "SerumPrefs.json"
    fallback = tmp_path / "fallback"
    monkeypatch.setattr(config, "_PREFS_PATHS", {"testos": {PresetFormat.SERUM1: str(prefs)}})
    monkeypatch.setattr(config, "_DEFAULT_PRESET_DIRS", {"testos": {PresetFormat.SERUM1: str(fallback)}})
    return prefs, fallback


def test_preset_dir_prefers_the_folder_serum_records(preset_env, tmp_path):
    """Serum's folder is relocatable from inside the plugin, so its own
    prefs entry beats the standard install location."""
    prefs, fallback = preset_env
    _preset_tree(fallback, ".fxp")
    moved = _preset_tree(tmp_path / "moved", ".fxp")
    prefs.write_text(json.dumps({"Serum Presets Path": f"{moved} "}))
    assert config.default_preset_dir(PresetFormat.SERUM1, "testos") == moved


def test_blank_prefs_value_falls_back(preset_env):
    """Serum 1 writes a single space when the folder was never moved."""
    prefs, fallback = preset_env
    _preset_tree(fallback, ".fxp")
    prefs.write_text(json.dumps({"Serum Presets Path": " "}))
    assert config.default_preset_dir(PresetFormat.SERUM1, "testos") == fallback


def test_directory_without_matching_presets_is_not_a_hit(preset_env):
    """A folder holding only the *other* format is a miss, not an empty batch."""
    _prefs, fallback = preset_env
    _preset_tree(fallback, ".SerumPreset")
    assert config.default_preset_dir(PresetFormat.SERUM1, "testos") is None


def test_uppercase_suffix_still_counts(preset_env):
    _prefs, fallback = preset_env
    _preset_tree(fallback, ".FXP")
    assert config.default_preset_dir(PresetFormat.SERUM1, "testos") == fallback


def test_malformed_prefs_falls_back_instead_of_raising(preset_env):
    prefs, fallback = preset_env
    _preset_tree(fallback, ".fxp")
    prefs.write_text("{not json")
    assert config.default_preset_dir(PresetFormat.SERUM1, "testos") == fallback


def test_stale_prefs_path_falls_back(preset_env, tmp_path):
    prefs, fallback = preset_env
    _preset_tree(fallback, ".fxp")
    prefs.write_text(json.dumps({"Serum Presets Path": str(tmp_path / "deleted")}))
    assert config.default_preset_dir(PresetFormat.SERUM1, "testos") == fallback


def test_nothing_found_returns_none(preset_env):
    assert config.default_preset_dir(PresetFormat.SERUM1, "testos") is None


def test_unknown_platform_returns_none(preset_env):
    assert config.default_preset_dir(PresetFormat.SERUM1, "plan9") is None


def test_suffix_for_round_trips_every_format():
    for fmt in PresetFormat:
        assert format_for_path(Path(f"x{suffix_for(fmt)}")) is fmt

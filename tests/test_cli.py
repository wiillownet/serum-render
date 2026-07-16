"""
CliRunner coverage for serum_render.cli. These tests never reach the
worker pool — they assert argument parsing, validation, error messaging,
exit codes, default-plugin-path resolution, and the --dry-run path.

Output is read via `result.output` (the merged stream) so the suite is
portable across Click versions.

Default-path resolution is stubbed in most tests: this machine may have
real Serum installs, and the CLI would otherwise silently pick them up
and proceed past the validation being tested.
"""
from __future__ import annotations

from pathlib import Path

import mido
import pytest
from typer.testing import CliRunner

import serum_render.cli as cli
from serum_render.cli import app

runner = CliRunner()


def _touch(p: Path, data: bytes = b"") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


@pytest.fixture
def no_defaults(monkeypatch):
    """Make default-plugin-path resolution find nothing, so tests exercise
    the explicit-flag paths regardless of what's installed on this machine."""
    monkeypatch.setattr(cli, "default_plugin_path", lambda fmt, platform=None: None)


@pytest.fixture
def fake_env(tmp_path: Path):
    """A dummy Serum 1 plugin file + a nested preset dir — valid CLI inputs
    for dry-run."""
    plugin = tmp_path / "Serum.vst"
    _touch(plugin)
    presets = tmp_path / "presets"
    _touch(presets / "Leads" / "lead.fxp")
    _touch(presets / "Bass" / "bass.fxp")
    output = tmp_path / "out"
    return plugin, presets, output


# ---- argument validation --------------------------------------------------


def test_note_and_midi_mutually_exclusive(fake_env, tmp_path, no_defaults):
    plugin, presets, output = fake_env
    midi = tmp_path / "seq.mid"
    mid = mido.MidiFile(type=1)
    mid.tracks.append(mido.MidiTrack())
    mid.save(str(midi))

    result = runner.invoke(
        app, [
            str(presets), str(output),
            "--serum1", str(plugin),
            "--note", "60",
            "--midi", str(midi),
        ]
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_bad_bit_depth(fake_env, no_defaults):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [str(presets), str(output), "--serum1", str(plugin), "--bit-depth", "8"]
    )
    assert result.exit_code != 0
    assert "must be 16, 24, or 32f" in result.output


def test_bad_format(fake_env, no_defaults):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [str(presets), str(output), "--serum1", str(plugin), "--format", "mp3"]
    )
    assert result.exit_code != 0
    assert "must be wav or npy" in result.output


def test_duration_zero_rejected(fake_env, no_defaults):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [str(presets), str(output), "--serum1", str(plugin), "--duration", "0"]
    )
    assert result.exit_code != 0
    assert "duration must be > 0" in result.output


def test_tail_zero_accepted_in_dry_run(fake_env, no_defaults):
    # tail=0 is valid (percussive) and must not error out.
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [
            str(presets), str(output),
            "--serum1", str(plugin),
            "--tail", "0",
            "--dry-run",
        ]
    )
    assert result.exit_code == 0, result.output


def test_sample_rate_zero_rejected(fake_env, no_defaults):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [
            str(presets), str(output),
            "--serum1", str(plugin),
            "--sample-rate", "0",
        ]
    )
    assert result.exit_code != 0
    # Typer surfaces `min=1` violations with an "Invalid value" range message.
    assert "Invalid value" in result.output and "sample-rate" in result.output


# ---- plugin resolution: flags + defaults -----------------------------------


def test_no_flag_and_no_default_rejected(tmp_path, no_defaults):
    """No --serum1 and no default install found -> clean flag-language error."""
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    result = runner.invoke(app, [str(presets), str(tmp_path / "out")])
    assert result.exit_code == 2
    assert ".fxp" in result.output
    assert "--serum1" in result.output
    assert "no default" in result.output


def test_default_plugin_used_when_flag_omitted(tmp_path, monkeypatch):
    """With a (stubbed) default install present, the flag is optional and
    the CLI announces which default it picked."""
    fake_default = tmp_path / "DefaultSerum.vst"
    _touch(fake_default)
    monkeypatch.setattr(
        cli, "default_plugin_path", lambda fmt, platform=None: fake_default
    )
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    result = runner.invoke(
        app, [str(presets), str(tmp_path / "out"), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Using default Serum 1 plugin" in result.output
    assert str(fake_default) in result.output


def test_explicit_flag_beats_default(tmp_path, monkeypatch):
    """--serum1 wins over an existing default; no default announcement."""
    fake_default = tmp_path / "DefaultSerum.vst"
    _touch(fake_default)
    monkeypatch.setattr(
        cli, "default_plugin_path", lambda fmt, platform=None: fake_default
    )
    explicit = tmp_path / "MySerum.vst"
    _touch(explicit)
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    result = runner.invoke(
        app, [str(presets), str(tmp_path / "out"), "--serum1", str(explicit), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Using default" not in result.output


def test_default_only_resolved_for_discovered_formats(tmp_path, monkeypatch):
    """An fxp-only preset dir must not resolve (or announce) a Serum 2
    default — no unused synth gets booted."""
    fake_default = tmp_path / "Default.vst"
    _touch(fake_default)
    monkeypatch.setattr(
        cli, "default_plugin_path", lambda fmt, platform=None: fake_default
    )
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    result = runner.invoke(
        app, [str(presets), str(tmp_path / "out"), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Serum 1 plugin" in result.output
    assert "Serum 2 plugin" not in result.output


def test_serum2_files_without_serum2_flag(tmp_path, no_defaults):
    """Discovering .SerumPreset files without --serum2 (and no default)
    must error naming the missing flag, not silently dispatch them."""
    plugin = tmp_path / "Serum.vst"
    _touch(plugin)
    presets = tmp_path / "presets"
    _touch(presets / "a.SerumPreset")
    result = runner.invoke(
        app, [str(presets), str(tmp_path / "out"), "--serum1", str(plugin)]
    )
    assert result.exit_code == 2
    assert ".SerumPreset" in result.output
    assert "--serum2" in result.output


def test_fxp_files_without_serum1_flag(tmp_path, no_defaults):
    plugin = tmp_path / "Serum2.vst3"
    _touch(plugin)
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    result = runner.invoke(
        app, [str(presets), str(tmp_path / "out"), "--serum2", str(plugin)]
    )
    assert result.exit_code == 2
    assert ".fxp" in result.output
    assert "--serum1" in result.output


def test_mixed_dir_requires_both_flags(tmp_path, no_defaults):
    plugin = tmp_path / "Serum.vst"
    _touch(plugin)
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    _touch(presets / "b.SerumPreset")
    result = runner.invoke(
        app, [str(presets), str(tmp_path / "out"), "--serum1", str(plugin)]
    )
    assert result.exit_code == 2
    assert "--serum2" in result.output


def test_mixed_dir_with_both_flags_passes_validation(tmp_path, no_defaults):
    serum1_plugin = tmp_path / "Serum.vst"
    serum2_plugin = tmp_path / "Serum2.vst3"
    _touch(serum1_plugin)
    _touch(serum2_plugin)
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    _touch(presets / "b.SerumPreset")
    result = runner.invoke(
        app, [
            str(presets), str(tmp_path / "out"),
            "--serum1", str(serum1_plugin),
            "--serum2", str(serum2_plugin),
            "--dry-run",
        ]
    )
    assert result.exit_code == 0, result.output
    assert "a.fxp" in result.output
    assert "b.SerumPreset" in result.output


# ---- path validation ------------------------------------------------------


def test_missing_serum1_plugin(tmp_path, no_defaults):
    presets = tmp_path / "presets"
    _touch(presets / "a.fxp")
    result = runner.invoke(
        app, [
            str(presets),
            str(tmp_path / "out"),
            "--serum1", str(tmp_path / "nope.vst"),
        ]
    )
    assert result.exit_code == 2
    assert "Plugin not found" in result.output


def test_missing_serum2_plugin(tmp_path, no_defaults):
    presets = tmp_path / "presets"
    _touch(presets / "a.SerumPreset")
    result = runner.invoke(
        app, [
            str(presets),
            str(tmp_path / "out"),
            "--serum2", str(tmp_path / "nope.vst3"),
        ]
    )
    assert result.exit_code == 2
    assert "Plugin not found" in result.output


def test_missing_presets(tmp_path, no_defaults):
    plugin = tmp_path / "Serum.vst"
    _touch(plugin)
    result = runner.invoke(
        app, [
            str(tmp_path / "nope"),
            str(tmp_path / "out"),
            "--serum1", str(plugin),
        ]
    )
    assert result.exit_code == 2
    assert "Presets path not found" in result.output


def test_output_is_existing_file(fake_env, tmp_path, no_defaults):
    plugin, presets, _ = fake_env
    existing_file = tmp_path / "not_a_dir.txt"
    existing_file.write_bytes(b"hello")
    result = runner.invoke(
        app, [str(presets), str(existing_file), "--serum1", str(plugin)]
    )
    assert result.exit_code == 2
    assert "not a directory" in result.output


def test_no_preset_files_found_in_presets_dir(tmp_path, no_defaults):
    plugin = tmp_path / "Serum.vst"
    _touch(plugin)
    empty_presets = tmp_path / "empty_presets"
    empty_presets.mkdir()
    result = runner.invoke(
        app, [
            str(empty_presets),
            str(tmp_path / "out"),
            "--serum1", str(plugin),
        ]
    )
    # Warning + exit 0 when nothing matches.
    assert result.exit_code == 0
    assert "No supported preset files" in result.output


# ---- MIDI error handling --------------------------------------------------


def test_missing_midi_file(fake_env, tmp_path, no_defaults):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [
            str(presets), str(output),
            "--serum1", str(plugin),
            "--midi", str(tmp_path / "nope.mid"),
        ]
    )
    assert result.exit_code == 2
    assert "MIDI file not found" in result.output


def test_type2_midi_file_clean_error(fake_env, tmp_path, no_defaults):
    plugin, presets, output = fake_env
    type2_midi = tmp_path / "type2.mid"
    mid = mido.MidiFile(type=2)
    mid.tracks.append(mido.MidiTrack())
    mid.save(str(type2_midi))

    result = runner.invoke(
        app, [
            str(presets), str(output),
            "--serum1", str(plugin),
            "--midi", str(type2_midi),
        ]
    )
    assert result.exit_code == 2
    assert "Type 2" in result.output
    # No raw traceback should leak through.
    assert "Traceback" not in result.output


def test_corrupt_midi_file_clean_error(fake_env, tmp_path, no_defaults):
    plugin, presets, output = fake_env
    bad = tmp_path / "bad.mid"
    bad.write_bytes(b"not a midi file")

    result = runner.invoke(
        app, [
            str(presets), str(output),
            "--serum1", str(plugin),
            "--midi", str(bad),
        ]
    )
    assert result.exit_code == 2
    assert "Error reading MIDI file" in result.output
    assert "Traceback" not in result.output


# ---- --dry-run + relative-path {subpath} regression -----------------------


def test_dry_run_prints_plan_without_rendering(fake_env, no_defaults):
    plugin, presets, output = fake_env
    result = runner.invoke(
        app, [str(presets), str(output), "--serum1", str(plugin), "--dry-run"]
    )
    assert result.exit_code == 0
    out = result.output
    assert "Would render" in out
    # Pin both sides of the input -> output mapping.
    assert " -> " in out
    assert "lead.fxp" in out and "lead.wav" in out
    assert "bass.fxp" in out and "bass.wav" in out
    # Output directory must not have been created when --dry-run.
    assert not output.exists()


def test_dry_run_with_relative_presets_dir_resolves_subpath(
    tmp_path, monkeypatch, no_defaults
):
    """Regression pin from vst-render: a relative PRESETS arg must still
    produce a non-empty {subpath} — the CLI must resolve presets_root
    before composing filenames, or relative_to() fails silently."""
    plugin = tmp_path / "Serum.vst"
    plugin.write_bytes(b"")
    (tmp_path / "presets" / "Leads").mkdir(parents=True)
    (tmp_path / "presets" / "Leads" / "lead.fxp").write_bytes(b"")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "presets",  # relative — the exact shape the bug needed
            "out",
            "--serum1", str(plugin),
            "--filename-template",
            "{subpath}_{preset}",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Leads_lead.wav" in result.output

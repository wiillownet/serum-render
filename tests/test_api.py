"""Unit coverage for the Renderer / ParallelRenderer guards that don't
require a plugin. End-to-end behavior is covered by the smoke tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from serum_render import ParallelRenderer, Renderer, RenderConfig
from serum_render.api import _build_job, _check_format_coverage
from serum_render.formats import PresetFormat


def _fake_plugin(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_bytes(b"")
    return p


# ---- context-manager guards -----------------------------------------------


def test_renderer_render_outside_context_raises(tmp_path):
    cfg = RenderConfig(serum1_plugin_path=_fake_plugin(tmp_path, "Serum.vst"))
    r = Renderer(cfg)
    with pytest.raises(RuntimeError, match="context manager"):
        r.render(tmp_path / "p.fxp")


def test_parallel_renderer_build_jobs_outside_context(tmp_path):
    cfg = RenderConfig(serum1_plugin_path=_fake_plugin(tmp_path, "Serum.vst"))
    r = ParallelRenderer(cfg, workers=1)
    with pytest.raises(RuntimeError, match="context manager"):
        r._build_jobs([Path("p.fxp")])


# ---- entry validation -------------------------------------------------------


def test_parallel_enter_rejects_missing_plugin_path(tmp_path):
    cfg = RenderConfig(serum1_plugin_path=tmp_path / "not_installed.vst")
    with pytest.raises(FileNotFoundError, match="Plugin not found"):
        ParallelRenderer(cfg).__enter__()


def test_parallel_enter_rejects_missing_midi_file(tmp_path):
    cfg = RenderConfig(
        serum1_plugin_path=_fake_plugin(tmp_path, "Serum.vst"),
        midi_path=tmp_path / "missing.mid",
    )
    with pytest.raises(FileNotFoundError, match="MIDI file not found"):
        ParallelRenderer(cfg).__enter__()


# ---- format auto-detection in _build_jobs ----------------------------------


def test_build_jobs_tags_fxp_path_with_serum1_format(tmp_path):
    cfg = RenderConfig(serum1_plugin_path=_fake_plugin(tmp_path, "Serum.vst"))
    with ParallelRenderer(cfg, workers=1) as r:
        jobs = r._build_jobs([tmp_path / "lead.fxp"])
    assert jobs[0].format is PresetFormat.SERUM1


def test_build_jobs_tags_serum_preset_path_with_serum2_format(tmp_path):
    cfg = RenderConfig(serum2_plugin_path=_fake_plugin(tmp_path, "Serum2.vst3"))
    with ParallelRenderer(cfg, workers=1) as r:
        jobs = r._build_jobs([tmp_path / "pad.SerumPreset"])
    assert jobs[0].format is PresetFormat.SERUM2


def test_build_jobs_tags_mixed_paths_per_path(tmp_path):
    """Format must be detected per path, not per batch."""
    cfg = RenderConfig(
        serum1_plugin_path=_fake_plugin(tmp_path, "Serum.vst"),
        serum2_plugin_path=_fake_plugin(tmp_path, "Serum2.vst3"),
    )
    with ParallelRenderer(cfg, workers=1) as r:
        jobs = r._build_jobs([tmp_path / "a.fxp", tmp_path / "b.SerumPreset"])
    assert [j.format for j in jobs] == [PresetFormat.SERUM1, PresetFormat.SERUM2]


def test_build_jobs_rejects_unknown_suffix(tmp_path):
    cfg = RenderConfig(serum1_plugin_path=_fake_plugin(tmp_path, "Serum.vst"))
    with ParallelRenderer(cfg, workers=1) as r:
        with pytest.raises(ValueError, match="Unsupported preset suffix"):
            r._build_jobs([tmp_path / "weird.vital"])


def test_build_jobs_carries_config_values(tmp_path):
    cfg = RenderConfig(
        serum1_plugin_path=_fake_plugin(tmp_path, "Serum.vst"),
        note=60,
        velocity=100,
        duration=0.25,
        tail=2.0,
    )
    with ParallelRenderer(cfg, workers=1) as r:
        job = r._build_jobs([tmp_path / "p.fxp"])[0]
    assert (job.note, job.velocity, job.duration, job.tail) == (60, 100, 0.25, 2.0)
    assert Path(job.preset_path).is_absolute()


# ---- format coverage check --------------------------------------------------


def test_coverage_passes_when_format_has_path(tmp_path):
    cfg = RenderConfig(serum1_plugin_path=_fake_plugin(tmp_path, "Serum.vst"))
    _check_format_coverage(cfg, {PresetFormat.SERUM1})  # no raise


def test_coverage_rejects_serum2_without_serum2_path(tmp_path):
    cfg = RenderConfig(serum1_plugin_path=_fake_plugin(tmp_path, "Serum.vst"))
    with pytest.raises(ValueError, match=r"\.SerumPreset.*serum2_plugin_path"):
        _check_format_coverage(cfg, {PresetFormat.SERUM2})


def test_coverage_rejects_fxp_without_serum1_path(tmp_path):
    cfg = RenderConfig(serum2_plugin_path=_fake_plugin(tmp_path, "Serum2.vst3"))
    with pytest.raises(ValueError, match=r"\.fxp.*serum1_plugin_path"):
        _check_format_coverage(cfg, {PresetFormat.SERUM1})


def test_coverage_lists_both_missing_in_error():
    """If both formats are required but neither is set, the error must
    name both fields so the user fixes them in one pass. RenderConfig
    can't normally be built with neither path, so skip __init__."""
    cfg = object.__new__(RenderConfig)
    object.__setattr__(cfg, "serum1_plugin_path", None)
    object.__setattr__(cfg, "serum2_plugin_path", None)
    with pytest.raises(ValueError) as excinfo:
        _check_format_coverage(cfg, {PresetFormat.SERUM1, PresetFormat.SERUM2})
    msg = str(excinfo.value)
    assert "serum1_plugin_path" in msg and "serum2_plugin_path" in msg


def test_render_checks_coverage_before_engine(tmp_path):
    """A serum2-only Renderer handed an .fxp path must fail with the
    coverage error naming the config field, before touching the host."""
    cfg = RenderConfig(serum2_plugin_path=_fake_plugin(tmp_path, "Serum2.vst3"))
    r = Renderer(cfg)
    # Simulate an entered renderer without loading a real plugin.
    r._host = object()
    with pytest.raises(ValueError, match="serum1_plugin_path"):
        r.render(tmp_path / "lead.fxp")


# ---- single-file job build ---------------------------------------------------


def test_build_job_resolves_midi_path(tmp_path):
    midi = tmp_path / "seq.mid"
    midi.write_bytes(b"")
    cfg = RenderConfig(
        serum1_plugin_path=_fake_plugin(tmp_path, "Serum.vst"), midi_path=midi
    )
    job = _build_job(cfg, tmp_path / "p.fxp", midi_duration=2.5)
    assert job.midi_path == str(midi.resolve())
    assert job.midi_duration == 2.5


def test_serum2_only_config_accepted_by_renderer(tmp_path):
    """Construction is cheap and must not gate on serum1_plugin_path."""
    cfg = RenderConfig(serum2_plugin_path=_fake_plugin(tmp_path, "Serum2.vst3"))
    Renderer(cfg)
    ParallelRenderer(cfg)

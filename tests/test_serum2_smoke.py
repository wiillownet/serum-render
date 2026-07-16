"""Serum 2 + mixed-format smoke tests: real plugins, real presets.

Covers the three entry shapes:
 - typed Jobs through pool.iter_jobs with output_path set (the CLI path)
 - ParallelRenderer.render_batch mixed-format (the library path)
 - the CLI itself, end-to-end via CliRunner

Each test gates on independent fixtures so a user with only one plugin
runs the smoke half they have plumbing for.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from typer.testing import CliRunner

from serum_render import ParallelRenderer, Renderer, RenderConfig
from serum_render.cli import app
from serum_render.config import SILENCE_EPS
from serum_render.formats import PresetFormat
from serum_render.jobs import Job
from serum_render.pool import iter_jobs


def _check_wav(wav_path: Path) -> None:
    audio, sample_rate = sf.read(str(wav_path))
    assert sample_rate == 44100, f"{wav_path}: sample rate {sample_rate} != 44100"
    assert audio.ndim == 2 and audio.shape[1] == 2, (
        f"{wav_path}: expected stereo, got shape {audio.shape}"
    )
    assert np.max(np.abs(audio)) > SILENCE_EPS, f"{wav_path}: silent output"


@pytest.mark.slow
def test_serum2_jobs_to_disk(serum2_plugin_path, serum2_preset_files, tmp_path):
    """Typed Jobs with output_path through the pool — validates the
    convert_preset_file -> write_bytes -> load_state round-trip and the
    disk-writing worker task end-to-end."""
    jobs = [
        Job(
            preset_path=src,
            format=PresetFormat.SERUM2,
            output_path=str(tmp_path / f"out_{i}.wav"),
        )
        for i, src in enumerate(serum2_preset_files)
    ]
    results = list(iter_jobs(jobs, 2, None, serum2_plugin_path, 44100))
    assert [r["status"] for r in results] == ["ok", "ok"], results
    for i in range(len(jobs)):
        _check_wav(tmp_path / f"out_{i}.wav")


@pytest.mark.slow
def test_mixed_batch_parallel(
    serum1_plugin_path,
    serum2_plugin_path,
    serum1_preset_files,
    serum2_preset_files,
):
    """One ParallelRenderer batch containing both formats."""
    config = RenderConfig(
        serum1_plugin_path=serum1_plugin_path,
        serum2_plugin_path=serum2_plugin_path,
        duration=0.5,
        tail=0.5,
    )
    batch = serum1_preset_files + serum2_preset_files
    with ParallelRenderer(config, workers=2) as renderer:
        results = renderer.render_batch(batch)

    assert len(results) == len(batch)
    for path, audio in results.items():
        assert audio.shape[0] == 2
        assert np.max(np.abs(audio)) > SILENCE_EPS, f"{path}: silent output"


@pytest.mark.slow
def test_sequential_renderer_mixed(
    serum1_plugin_path,
    serum2_plugin_path,
    serum1_preset_files,
    serum2_preset_files,
):
    """Sequential Renderer alternating formats on one EngineHost."""
    config = RenderConfig(
        serum1_plugin_path=serum1_plugin_path,
        serum2_plugin_path=serum2_plugin_path,
        duration=0.5,
        tail=0.5,
    )
    with Renderer(config) as renderer:
        for path in [
            serum1_preset_files[0],
            serum2_preset_files[0],
            serum1_preset_files[1],
            serum2_preset_files[1],
        ]:
            audio = renderer.render(path)
            assert np.max(np.abs(audio)) > SILENCE_EPS, f"{path}: silent output"


@pytest.mark.slow
def test_cli_end_to_end_mixed(
    serum1_plugin_path,
    serum2_plugin_path,
    serum1_preset_files,
    serum2_preset_files,
    tmp_path,
):
    """The actual CLI over a mixed-format directory."""
    presets = tmp_path / "presets"
    presets.mkdir()
    for src in serum1_preset_files + serum2_preset_files:
        (presets / Path(src).name).write_bytes(Path(src).read_bytes())
    out = tmp_path / "out"

    result = CliRunner().invoke(
        app,
        [
            str(presets), str(out),
            "--serum1", serum1_plugin_path,
            "--serum2", serum2_plugin_path,
            "--duration", "0.5",
            "--tail", "0.5",
            "--workers", "2",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "0 failed" in result.output
    wavs = sorted(out.glob("*.wav"))
    assert len(wavs) == 4, [w.name for w in wavs]
    for w in wavs:
        _check_wav(w)

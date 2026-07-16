"""End-to-end Serum 1 smoke: real plugin, real .fxp presets. Run with
--serum1-plugin-path / --serum1-preset-dir (or env vars)."""
from __future__ import annotations

from pathlib import Path

import mido
import numpy as np
import pytest

from serum_render import ParallelRenderer, RenderConfig


@pytest.mark.slow
def test_parallel_render_produces_audio(serum1_plugin_path, serum1_preset_files):
    config = RenderConfig(
        serum1_plugin_path=serum1_plugin_path,
        sample_rate=44100,
        note=48,
        velocity=127,
        duration=1.0,
        tail=1.0,
    )
    with ParallelRenderer(config, workers=2) as renderer:
        results = renderer.render_batch(serum1_preset_files)

    assert len(results) == len(serum1_preset_files)
    for path, audio in results.items():
        assert audio.shape[0] == 2, f"{path}: expected stereo"
        assert audio.dtype == np.float32
        assert np.max(np.abs(audio)) > 3.16e-5, f"{path}: audio is silent"


def _write_midi_sequence(path: Path, beats: int) -> None:
    """One held note of `beats` beats at default tempo (120 BPM, 480
    ticks/beat -> 0.5 s/beat). Total duration = beats * 0.5."""
    mid = mido.MidiFile(type=1)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.Message("note_on", note=60, velocity=100, time=0))
    track.append(mido.Message("note_off", note=60, velocity=64, time=beats * 480))
    mid.save(str(path))


@pytest.mark.slow
def test_parallel_render_with_midi_file(
    serum1_plugin_path, serum1_preset_files, tmp_path
):
    """MIDI mode: workers must render for (midi_duration + tail) seconds,
    not (duration + tail)."""
    midi_path = tmp_path / "seq.mid"
    _write_midi_sequence(midi_path, beats=4)  # 4 beats @ 120 BPM = 2.0 s
    expected_duration = 2.0 + 0.5
    sample_rate = 44100

    config = RenderConfig(
        serum1_plugin_path=serum1_plugin_path,
        sample_rate=sample_rate,
        midi_path=midi_path,
        tail=0.5,
        # note/duration must not affect the render when midi_path is set.
        note=48,
        duration=0.1,
    )
    with ParallelRenderer(config, workers=2) as renderer:
        results = renderer.render_batch(serum1_preset_files)

    assert len(results) == len(serum1_preset_files)
    for path, audio in results.items():
        assert np.max(np.abs(audio)) > 3.16e-5, f"{path}: silent output"
        actual_seconds = audio.shape[1] / sample_rate
        # DawDreamer rounds render duration up to the next 512-sample buffer.
        assert abs(actual_seconds - expected_duration) < 0.05, (
            f"{path}: expected ~{expected_duration:.2f}s, got {actual_seconds:.2f}s"
        )
        assert actual_seconds > 1.0, (
            f"{path}: got {actual_seconds:.2f}s — MIDI file not loaded?"
        )

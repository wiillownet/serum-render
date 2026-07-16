import dataclasses

import pytest

from serum_render.formats import PresetFormat
from serum_render.jobs import Job


def test_minimal_job():
    job = Job(preset_path="/abs/p.fxp", format=PresetFormat.SERUM1)
    assert job.note == 48
    assert job.velocity == 127
    assert job.duration == 1.0
    assert job.tail == 1.0
    assert job.midi_path is None
    assert job.output_path is None
    assert job.skip_existing is False


def test_job_is_frozen():
    job = Job(preset_path="/abs/p.fxp", format=PresetFormat.SERUM1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        job.note = 60


def test_midi_path_without_duration_rejected():
    # Pins the invariant: workers never parse MIDI, so a Job carrying a
    # midi_path must also carry the precomputed duration. Without this
    # guard the failure is a confusing `None + float` TypeError deep in
    # the render loop.
    with pytest.raises(ValueError, match="midi_duration"):
        Job(
            preset_path="/abs/p.fxp",
            format=PresetFormat.SERUM1,
            midi_path="/abs/seq.mid",
        )


def test_midi_path_with_duration_accepted():
    job = Job(
        preset_path="/abs/p.fxp",
        format=PresetFormat.SERUM1,
        midi_path="/abs/seq.mid",
        midi_duration=2.5,
    )
    assert job.midi_duration == 2.5


def test_job_roundtrips_through_pickle():
    # Jobs cross the loky process boundary; plain pickle is a stricter
    # subset of cloudpickle, so this pins the wire-format assumption.
    import pickle

    job = Job(preset_path="/abs/p.SerumPreset", format=PresetFormat.SERUM2)
    clone = pickle.loads(pickle.dumps(job))
    assert clone == job
    assert clone.format is PresetFormat.SERUM2

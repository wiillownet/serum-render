from pathlib import Path

import mido
import pytest

from serum_render.discover import get_midi_duration


def _make_midi(path: Path, file_type: int, ticks: int = 480) -> None:
    """Write a minimal valid MIDI file at the given type (0, 1, or 2)."""
    mid = mido.MidiFile(type=file_type)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.Message("note_on", note=60, velocity=64, time=0))
    track.append(mido.Message("note_off", note=60, velocity=64, time=ticks))
    mid.save(str(path))


def test_duration_positive_for_valid_file(tmp_path: Path):
    path = tmp_path / "ok.mid"
    _make_midi(path, file_type=1, ticks=480)
    # Default tempo 500000 us/beat (120 BPM), default 480 ticks/beat
    # -> 480 ticks = 1 beat = 0.5s. Allow slack for mido's internal rounding.
    duration = get_midi_duration(path)
    assert 0.4 <= duration <= 0.6


def test_duration_scales_with_ticks(tmp_path: Path):
    short = tmp_path / "short.mid"
    long = tmp_path / "long.mid"
    _make_midi(short, file_type=1, ticks=480)
    _make_midi(long, file_type=1, ticks=480 * 4)
    assert get_midi_duration(long) > get_midi_duration(short)


def test_type_2_raises_typeerror(tmp_path: Path):
    path = tmp_path / "type2.mid"
    _make_midi(path, file_type=2)
    with pytest.raises(TypeError, match="Type 2"):
        get_midi_duration(path)


def test_invalid_file_raises_valueerror(tmp_path: Path):
    path = tmp_path / "bad.mid"
    path.write_bytes(b"this is not a midi file")
    with pytest.raises(ValueError):
        get_midi_duration(path)


def test_nonexistent_file_raises_valueerror(tmp_path: Path):
    # A missing file surfaces as ValueError (wrapping mido's FileNotFoundError)
    with pytest.raises(ValueError):
        get_midi_duration(tmp_path / "nope.mid")


def test_type_0_accepted(tmp_path: Path):
    path = tmp_path / "type0.mid"
    _make_midi(path, file_type=0)
    duration = get_midi_duration(path)
    assert duration > 0

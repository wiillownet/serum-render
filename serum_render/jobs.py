"""Frozen Job dataclass — the unit of work an EngineHost renders.

Stdlib-only at module level: Jobs cross the loky boundary, so this
module is imported inside worker processes before dawdreamer loads.
"""
from __future__ import annotations

from dataclasses import dataclass

from .formats import PresetFormat


@dataclass(frozen=True)
class Job:
    preset_path: str  # absolute path string
    format: PresetFormat
    note: int = 48
    velocity: int = 127
    duration: float = 1.0
    tail: float = 1.0
    midi_path: str | None = None  # absolute path string
    # Computed once in the main process (discover.get_midi_duration);
    # workers never parse MIDI. Required whenever midi_path is set.
    midi_duration: float | None = None
    # Set for disk-writing batch jobs (CLI); None means "return the audio".
    output_path: str | None = None
    bit_depth: str = "16"  # "16" | "24" | "32f" — only used when writing
    output_format: str = "wav"  # "wav" | "npy" — only used when writing
    skip_existing: bool = False

    def __post_init__(self) -> None:
        if self.midi_path is not None and self.midi_duration is None:
            raise ValueError(
                "midi_duration must be set when midi_path is set; compute it "
                "once in the main process via discover.get_midi_duration"
            )

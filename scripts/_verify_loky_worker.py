"""Worker functions for verify_dawdreamer.py's loky test (test 2).

These live in their own importable module rather than in the script's
__main__: cloudpickle round-trips __main__-defined functions through a
private namespace, so the module-level engine that init_worker assigns
would not be the one render_task reads.

dawdreamer must be the first non-stdlib import in a rendering process,
so both third-party imports are function-local.
"""
from __future__ import annotations

import os

# One engine + synth per worker process, built once by the initializer.
_ENGINE = None
_SYNTH = None


def init_worker(plugin_path: str, sample_rate: int) -> None:
    """loky initializer — build the per-process engine."""
    global _ENGINE, _SYNTH
    import dawdreamer as daw

    _ENGINE = daw.RenderEngine(sample_rate, 512)
    _SYNTH = _ENGINE.make_plugin_processor("serum", plugin_path)
    _ENGINE.load_graph([(_SYNTH, [])])


def render_task(preset_path: str) -> dict:
    """Render one preset. Returns the worker pid so the caller can tell
    which process served the task, and the peak so it can tell the render
    was not silent."""
    import numpy as np

    if _SYNTH is None or _ENGINE is None:
        raise RuntimeError("render_task called before init_worker")

    _SYNTH.load_preset(preset_path)
    _SYNTH.clear_midi()
    _SYNTH.add_midi_note(48, 127, 0.0, 1.0)
    _ENGINE.render(2.0)
    audio = _ENGINE.get_audio()
    return {"pid": os.getpid(), "peak": float(np.max(np.abs(audio)))}


def kill_task() -> dict:
    """Hard-kill this worker from inside the task, simulating a plugin
    that takes the process down mid-render. os._exit skips interpreter
    cleanup and works on Windows, where there is no SIGKILL. Never
    returns; the return annotation matches render_task for the caller."""
    os._exit(1)

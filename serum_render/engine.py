"""
The single render core, used by every path: the sequential Renderer holds
an EngineHost in-process; loky workers hold one in a module global via
init_worker. There is no second implementation.

MUST be safe to import in a fresh worker process without triggering any
LLVM-adjacent library load ahead of dawdreamer — module-level imports are
restricted to stdlib + sibling stdlib-only modules. dawdreamer / numpy /
serum2_preset_loader load inside EngineHost, in that order.
"""
from __future__ import annotations

import logging
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import SILENCE_EPS
from .formats import PresetFormat
from .jobs import Job
from .output import write_audio

logger = logging.getLogger("serum_render")


@dataclass
class _FormatEngine:
    """One DawDreamer RenderEngine + synth for one preset format.

    Each format gets its OWN engine. Never put two synths into one
    engine's graph: `load_graph` with multiple orphan source processors
    routes only the last one to `get_audio()` and silently discards the
    rest (vst-render audit-log 2026-05-20).
    """
    engine: object
    synth: object
    # serum2 only: per-process tempfile that round-trips the converted
    # state blob into `synth.load_state` (which takes a path, not bytes).
    # Reused for every job — write_bytes overwrites in place. Never
    # shared across processes.
    state_path: Path | None = None


class EngineHost:
    """Builds one engine per supplied format, once, and renders Jobs.

    Rules inherited from vst-render (see CLAUDE.md "Critical constraints"):
    - dawdreamer is imported here, first, before numpy.
    - Engines are built once per process; `load_preset` / `load_state`
      hot-swap presets in place. Never rebuild engines per job.
    - Each synth gets a 0.1s warmup render — Serum 2 lazy-loads sample
      data and its cold render comes out ~10x hot. Do not remove.
    - All paths handed to DawDreamer are absolute strings.
    """

    def __init__(
        self,
        serum1_plugin_path: str | Path | None,
        serum2_plugin_path: str | Path | None,
        sample_rate: int,
    ) -> None:
        # Cheap guard before the expensive dawdreamer import, so a unit
        # test can exercise it without paying (or reordering) the import.
        if serum1_plugin_path is None and serum2_plugin_path is None:
            raise ValueError(
                "EngineHost requires at least one of serum1_plugin_path or "
                "serum2_plugin_path"
            )

        import dawdreamer as daw  # MUST be first non-stdlib import
        import numpy  # noqa: F401  pin load order ahead of soundfile

        self.sample_rate = sample_rate
        self._engines: dict[PresetFormat, _FormatEngine] = {}

        if serum1_plugin_path is not None:
            self._engines[PresetFormat.SERUM1] = self._build_engine(
                daw, "serum1_synth", serum1_plugin_path, state_path=None
            )
        if serum2_plugin_path is not None:
            tmpdir = tempfile.mkdtemp(prefix="serum_render_")
            self._engines[PresetFormat.SERUM2] = self._build_engine(
                daw,
                "serum2_synth",
                serum2_plugin_path,
                state_path=Path(tmpdir) / "state.bin",
            )

    def _build_engine(
        self, daw, name: str, plugin_path: str | Path, state_path: Path | None
    ) -> _FormatEngine:
        engine = daw.RenderEngine(self.sample_rate, 512)
        synth = engine.make_plugin_processor(name, str(Path(plugin_path).resolve()))
        engine.load_graph([(synth, [])])
        # Warmup render — absorbs Serum 2's ~10x cold-start anomaly inside
        # host construction so the caller's first render is correct.
        synth.clear_midi()
        synth.add_midi_note(48, 127, 0.0, 0.05)
        engine.render(0.1)
        return _FormatEngine(engine=engine, synth=synth, state_path=state_path)

    def loaded_formats(self) -> set[PresetFormat]:
        return set(self._engines)

    def render(self, job: Job):
        """Render one Job, returning (channels, samples) float32 audio.

        Raises on failure — error-dict wrapping is the pool path's
        concern (run_job), not the engine's.
        """
        # DawDreamer hangs when driven from a non-main thread (JUCE
        # internals are not thread-safe). Loud check beats a silent hang.
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(
                "EngineHost.render() must be called from the main thread. "
                "DawDreamer hangs when used from threads. Use "
                "ParallelRenderer for concurrent rendering."
            )

        try:
            fmt = PresetFormat(job.format)
        except ValueError:
            raise ValueError(f"Unknown preset format: {job.format!r}") from None
        fe = self._engines.get(fmt)
        if fe is None:
            raise RuntimeError(
                f"Got a {fmt.value} job but this EngineHost has no "
                f"{fmt.value} engine — it was built without "
                f"{fmt.value}_plugin_path"
            )

        import numpy as np

        if fmt is PresetFormat.SERUM1:
            fe.synth.load_preset(job.preset_path)
        else:
            from serum2_preset_loader import convert_preset_file

            # write_bytes (not append) so blob-size variance across
            # presets can't leave stale tail bytes from a larger blob.
            fe.state_path.write_bytes(convert_preset_file(job.preset_path))
            fe.synth.load_state(str(fe.state_path))

        if job.midi_path is not None:
            fe.synth.load_midi(
                job.midi_path, clear_previous=True, beats=False, all_events=True
            )
            render_duration = job.midi_duration + job.tail
        else:
            fe.synth.clear_midi()
            fe.synth.add_midi_note(job.note, job.velocity, 0.0, job.duration)
            render_duration = job.duration + job.tail

        fe.engine.render(render_duration)
        audio = fe.engine.get_audio()  # (2, N) float32

        if np.max(np.abs(audio)) < SILENCE_EPS:
            logger.warning("Silent output for preset: %s", job.preset_path)

        return audio


# ---- loky worker glue -------------------------------------------------
# One EngineHost per worker process, created once by the pool initializer
# and reused for every job.

_HOST: EngineHost | None = None


def init_worker(
    serum1_plugin_path: str | None,
    serum2_plugin_path: str | None,
    sample_rate: int,
) -> None:
    """loky initializer — builds the per-process EngineHost."""
    global _HOST
    _HOST = EngineHost(serum1_plugin_path, serum2_plugin_path, sample_rate)
    logger.debug(
        "Worker initialized (serum1=%s, serum2=%s)",
        serum1_plugin_path is not None,
        serum2_plugin_path is not None,
    )


def run_job(job: Job) -> dict:
    """Pool task: render one Job.

    If `job.output_path` is set, write the audio to disk and return a
    small status dict (cheap IPC — the CLI path). Otherwise return the
    audio array in the dict (the library path; ~700 KB per 2s stereo
    render crosses the process boundary).

    Per-job errors become {"status": "error", ...} so one bad preset
    doesn't kill the batch.
    """
    try:
        if _HOST is None:
            raise RuntimeError("run_job called before init_worker")
        if (
            job.output_path is not None
            and job.skip_existing
            and Path(job.output_path).exists()
        ):
            return {"status": "skipped", "path": job.preset_path}

        audio = _HOST.render(job)
        if job.output_path is not None:
            write_audio(
                audio,
                job.output_path,
                _HOST.sample_rate,
                job.bit_depth,
                job.output_format,
            )
            return {"status": "ok", "path": job.preset_path}
        return {"status": "ok", "path": job.preset_path, "audio": audio}
    except Exception as exc:
        logger.warning("Failed to render %s: %s", job.preset_path, exc)
        return {"status": "error", "path": job.preset_path, "error": str(exc)}

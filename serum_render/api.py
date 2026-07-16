"""
Public library API: Renderer, ParallelRenderer, render_preset.

Thin wrappers over the single render core (engine.EngineHost) — no
render logic lives here. Module level stays stdlib-only so importing
the package stays cheap and worker-safe.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator

from .config import RenderConfig
from .discover import get_midi_duration
from .engine import EngineHost
from .formats import PresetFormat, format_for_path
from .jobs import Job

if TYPE_CHECKING:
    import numpy as np


def _validate_entry(config: RenderConfig) -> float | None:
    """First-use validation: any plugin/MIDI path the caller set must
    exist. Returns the precomputed MIDI duration (workers never parse
    MIDI) or None."""
    for path in (config.serum1_plugin_path, config.serum2_plugin_path):
        if path is not None and not Path(path).exists():
            raise FileNotFoundError(f"Plugin not found: {path}")
    if config.midi_path is not None:
        if not Path(config.midi_path).exists():
            raise FileNotFoundError(f"MIDI file not found: {config.midi_path}")
        return get_midi_duration(Path(config.midi_path))
    return None


def _check_format_coverage(
    config: RenderConfig, formats: Iterable[PresetFormat]
) -> None:
    """Every format actually being rendered must have its plugin path on
    the config. Fails before any engine boots, naming the missing field."""
    missing = sorted(
        f"{'.fxp' if fmt is PresetFormat.SERUM1 else '.SerumPreset'} preset(s) "
        f"supplied but RenderConfig.{fmt.value}_plugin_path is unset"
        for fmt in set(formats)
        if config.plugin_path_for(fmt) is None
    )
    if missing:
        raise ValueError("; ".join(missing))


def _build_job(
    config: RenderConfig, preset_path: str | Path, midi_duration: float | None
) -> Job:
    path = Path(preset_path)
    fmt = format_for_path(path)
    _check_format_coverage(config, [fmt])
    return Job(
        preset_path=str(path.resolve()),
        format=fmt,
        note=config.note,
        velocity=config.velocity,
        duration=config.duration,
        tail=config.tail,
        midi_path=(
            str(Path(config.midi_path).resolve())
            if config.midi_path is not None
            else None
        ),
        midi_duration=midi_duration,
    )


class Renderer:
    """
    Single-process, sequential renderer. Loads the configured plugin(s)
    once in `__enter__` and hot-swaps presets for every `render()` call.
    Preset format is auto-detected from the file suffix. Errors raise.
    """

    def __init__(self, config: RenderConfig):
        self.config = config
        self._host: EngineHost | None = None
        self._midi_duration: float | None = None
        self._entered = False

    def __enter__(self) -> "Renderer":
        self._midi_duration = _validate_entry(self.config)
        if not self.config.deterministic:
            # Deterministic mode never builds an in-process engine —
            # every render runs in its own single-use process.
            self._host = EngineHost(
                self.config.serum1_plugin_path,
                self.config.serum2_plugin_path,
                self.config.sample_rate,
            )
        self._entered = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # DawDreamer has no explicit teardown — drop the ref for GC.
        self._host = None
        self._entered = False

    def render(self, preset_path: str | Path) -> "np.ndarray":
        if not getattr(self, "_entered", False):
            raise RuntimeError("Renderer must be used as a context manager")
        job = _build_job(self.config, preset_path, self._midi_duration)
        if self.config.deterministic:
            from .pool import render_isolated

            cfg = self.config
            result = render_isolated(
                job,
                str(cfg.serum1_plugin_path) if cfg.serum1_plugin_path else None,
                str(cfg.serum2_plugin_path) if cfg.serum2_plugin_path else None,
                cfg.sample_rate,
                keep_audio=True,
            )
            if result["status"] != "ok":
                raise RuntimeError(
                    f"Deterministic render failed for {preset_path}: "
                    f"{result.get('error')}"
                )
            return result["audio"]
        return self._host.render(job)


class ParallelRenderer:
    """
    Multi-process renderer for bulk use. Mixed-format batches are fine —
    format is auto-detected per path. Audio ships back from workers to
    the main process (~700 KB per 2s stereo render); for very large
    libraries iterate and spill to disk instead of holding the dict.
    """

    def __init__(self, config: RenderConfig, workers: int = -1):
        self.config = config
        self.workers = workers
        self._midi_duration: float | None = None
        self._entered = False

    def __enter__(self) -> "ParallelRenderer":
        self._midi_duration = _validate_entry(self.config)
        self._entered = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Executor is owned by loky's reusable cache; leave it warm so
        # the next ParallelRenderer in this process reuses the workers.
        self._entered = False

    def _build_jobs(self, preset_paths: list[str | Path]) -> list[Job]:
        if not self._entered:
            raise RuntimeError(
                "ParallelRenderer must be used as a context manager"
            )
        # Coverage-check the whole batch first so the error names every
        # missing plugin path in one pass, before any worker boots.
        _check_format_coverage(
            self.config, [format_for_path(Path(p)) for p in preset_paths]
        )
        return [
            _build_job(self.config, p, self._midi_duration) for p in preset_paths
        ]

    def iter_batch(
        self, preset_paths: list[str | Path]
    ) -> Iterator[tuple[str, "np.ndarray"]]:
        """Yield `(preset_path, audio)` as each job completes (unordered).
        Failed jobs are logged by the worker and skipped here."""
        from .pool import iter_jobs, iter_jobs_isolated

        jobs = self._build_jobs(preset_paths)
        cfg = self.config
        serum1 = str(cfg.serum1_plugin_path) if cfg.serum1_plugin_path else None
        serum2 = str(cfg.serum2_plugin_path) if cfg.serum2_plugin_path else None
        if cfg.deterministic:
            results = iter_jobs_isolated(
                jobs, self.workers, serum1, serum2, cfg.sample_rate,
                keep_audio=True,
            )
        else:
            results = iter_jobs(jobs, self.workers, serum1, serum2, cfg.sample_rate)
        for result in results:
            if result["status"] == "ok":
                yield result["path"], result["audio"]

    def render_batch(
        self, preset_paths: list[str | Path]
    ) -> dict[str, "np.ndarray"]:
        """Render all presets and return a dict mapping path -> audio."""
        return dict(self.iter_batch(preset_paths))


def render_preset(preset_path: str | Path, config: RenderConfig) -> "np.ndarray":
    """
    One-off render. Spins up a fresh EngineHost, renders, returns audio.
    Not suitable for batch use — each call pays the ~1-2s plugin
    cold-start plus a 0.1s warmup render per loaded synth.
    """
    with Renderer(config) as renderer:
        return renderer.render(preset_path)

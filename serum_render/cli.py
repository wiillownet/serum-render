"""
Typer CLI entry point. Discovers presets, resolves plugin paths (explicit
flags beat platform defaults), builds typed Jobs, and drives the pool.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from .config import default_plugin_path
from .discover import (
    compose_filename,
    discover_presets,
    get_midi_duration,
    resolve_output_paths,
)
from .formats import PresetFormat
from .jobs import Job
from .pool import iter_jobs, resolve_worker_count

logger = logging.getLogger("serum_render")

app = typer.Typer(
    add_completion=False,
    help="Batch-render Serum presets (.fxp, .SerumPreset) to audio.",
)

_FLAG_FOR = {PresetFormat.SERUM1: "--serum1", PresetFormat.SERUM2: "--serum2"}
_EXT_FOR = {PresetFormat.SERUM1: ".fxp", PresetFormat.SERUM2: ".SerumPreset"}
_PLUGIN_NAME_FOR = {PresetFormat.SERUM1: "Serum 1", PresetFormat.SERUM2: "Serum 2"}


def _setup_logging(verbose: bool) -> None:
    """Only the CLI configures logging — library code uses a named logger."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command()
def render(
    presets: Path = typer.Argument(..., help="Path to a single preset (.fxp or .SerumPreset) or a directory of them."),
    output: Path = typer.Argument(..., help="Output directory (created if missing)."),
    serum1: Optional[Path] = typer.Option(
        None, "--serum1",
        help="Path to a Serum 1 plugin that loads .fxp presets — the VST2 "
             "binary (.dll on Windows, .vst bundle on macOS). Defaults to "
             "the standard install location if .fxp files are being "
             "rendered and it exists.",
    ),
    serum2: Optional[Path] = typer.Option(
        None, "--serum2",
        help="Path to the Serum 2 VST3 plugin. Defaults to the standard "
             "install location if .SerumPreset files are being rendered "
             "and it exists.",
    ),
    note: Optional[int] = typer.Option(None, min=0, max=127, help="MIDI note (0-127). Default 48 (C3)."),
    velocity: int = typer.Option(127, min=1, max=127, help="MIDI velocity (1-127)."),
    duration: float = typer.Option(1.0, help="Note-on duration in seconds (> 0)."),
    tail: float = typer.Option(1.0, min=0.0, help="Release silence in seconds (>= 0)."),
    sample_rate: int = typer.Option(44100, "--sample-rate", min=1, help="Output sample rate in Hz."),
    bit_depth: str = typer.Option("16", "--bit-depth", help="Output bit depth: 16, 24, or 32f."),
    fmt: str = typer.Option("wav", "--format", help="Output container: wav or npy."),
    filename_template: str = typer.Option(
        "{preset}", "--filename-template",
        help="Filename template. Vars: {preset} {note} {velocity} {folder} {subpath}.",
    ),
    midi: Optional[Path] = typer.Option(None, "--midi", help="Path to a .mid file (overrides --note)."),
    workers: int = typer.Option(-1, "--workers", help="Parallel workers. -1 = cpu_count - 1."),
    skip_existing: bool = typer.Option(False, "--skip-existing", help="Skip if output file already exists."),
    deterministic: bool = typer.Option(
        False, "--deterministic",
        help="Render every preset in a fresh single-use process so batch "
             "output is bit-reproducible. Slower: one plugin load per "
             "preset instead of per worker.",
    ),
    no_recurse: bool = typer.Option(False, "--no-recurse", help="Do not recurse into subdirectories."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print presets that would render and exit."),
    verbose: bool = typer.Option(False, "--verbose", help="Per-preset status logging."),
) -> None:
    _setup_logging(verbose)

    # --note and --midi are mutually exclusive. Typer can't detect a
    # user-set default, so we use None sentinel + manual check.
    if midi is not None and note is not None:
        raise typer.BadParameter(
            "--note and --midi are mutually exclusive. Use --midi to render a "
            "MIDI sequence, or --note to render a single note."
        )
    if note is None:
        note = 48

    # Typer's `min=` is inclusive, so "> 0" on duration needs a manual check.
    if duration <= 0:
        raise typer.BadParameter(f"--duration must be > 0 (got {duration}).")

    if bit_depth not in ("16", "24", "32f"):
        raise typer.BadParameter(f"--bit-depth must be 16, 24, or 32f (got {bit_depth!r}).")
    if fmt not in ("wav", "npy"):
        raise typer.BadParameter(f"--format must be wav or npy (got {fmt!r}).")

    # Path.exists() returns True for VST3 bundle directories on macOS and
    # for plain .vst3 / .dll / .vst files on Windows + macOS — both shapes
    # are valid plugin paths, so no is_file() check.
    if serum1 is not None and not serum1.exists():
        typer.echo(f"Plugin not found: {serum1}", err=True)
        raise typer.Exit(code=2)
    if serum2 is not None and not serum2.exists():
        typer.echo(f"Plugin not found: {serum2}", err=True)
        raise typer.Exit(code=2)
    if not presets.exists():
        typer.echo(f"Presets path not found: {presets}", err=True)
        raise typer.Exit(code=2)
    if output.exists() and not output.is_dir():
        typer.echo(f"Output path exists and is not a directory: {output}", err=True)
        raise typer.Exit(code=2)

    try:
        preset_files = discover_presets(presets, recurse=not no_recurse)
    except ValueError as exc:
        # Single-file mode with an unsupported extension.
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    if not preset_files:
        typer.echo(
            f"No supported preset files (.fxp, .SerumPreset) found under {presets}",
            err=True,
        )
        raise typer.Exit(code=0)

    # Resolve plugin paths: an explicit flag always wins; otherwise fall
    # back to the standard install location — but only for formats that
    # actually appear in the discovered set, and only if the default
    # exists on disk (a missing default is "unset", not an error).
    discovered_formats = {fmt_tag for _, fmt_tag in preset_files}
    plugin_paths: dict[PresetFormat, Path] = {}
    explicit = {PresetFormat.SERUM1: serum1, PresetFormat.SERUM2: serum2}
    for preset_fmt in discovered_formats:
        if explicit[preset_fmt] is not None:
            plugin_paths[preset_fmt] = explicit[preset_fmt]
        else:
            fallback = default_plugin_path(preset_fmt)
            if fallback is not None:
                plugin_paths[preset_fmt] = fallback
                typer.echo(
                    f"Using default {_PLUGIN_NAME_FOR[preset_fmt]} plugin: {fallback}"
                )

    missing = discovered_formats - set(plugin_paths)
    if missing:
        msgs = sorted(
            f"found {_EXT_FOR[m]} files but {_FLAG_FOR[m]} was not provided "
            f"and no default {_PLUGIN_NAME_FOR[m]} install was found"
            for m in missing
        )
        for m in msgs:
            typer.echo(m, err=True)
        raise typer.Exit(code=2)

    # Single-file mode: presets_root=None so {subpath} collapses out.
    # Resolve when a directory so `relative_to` works against the absolute
    # preset paths that discover_presets returns — a relative presets arg
    # would otherwise silently collapse {subpath} to an empty string.
    presets_root: Path | None = presets.resolve() if presets.is_dir() else None

    # Compute MIDI duration once in the main process — all workers share it.
    midi_duration: float | None = None
    midi_str: str | None = None
    if midi is not None:
        if not midi.exists():
            typer.echo(f"MIDI file not found: {midi}", err=True)
            raise typer.Exit(code=2)
        try:
            midi_duration = get_midi_duration(midi)
        except (TypeError, ValueError) as exc:
            typer.echo(f"Error reading MIDI file '{midi}': {exc}", err=True)
            raise typer.Exit(code=2) from None
        midi_str = str(midi.resolve())

    extension = ".npy" if fmt == "npy" else ".wav"
    stems = [
        compose_filename(filename_template, p, presets_root, note, velocity)
        for p, _ in preset_files
    ]
    output_paths = resolve_output_paths(stems, output, extension)
    jobs = [
        Job(
            preset_path=str(p.resolve()),
            format=preset_fmt,
            note=note,
            velocity=velocity,
            duration=duration,
            tail=tail,
            midi_path=midi_str,
            midi_duration=midi_duration,
            output_path=out,
            bit_depth=bit_depth,
            output_format=fmt,
            skip_existing=skip_existing,
        )
        for (p, preset_fmt), out in zip(preset_files, output_paths)
    ]

    if dry_run:
        typer.echo(f"Would render {len(jobs)} preset(s):")
        for j in jobs:
            typer.echo(f"  {j.preset_path}  ->  {j.output_path}")
        raise typer.Exit(code=0)

    output.mkdir(parents=True, exist_ok=True)
    n_workers = resolve_worker_count(workers)
    serum1_str = (
        str(plugin_paths[PresetFormat.SERUM1].resolve())
        if PresetFormat.SERUM1 in plugin_paths
        else None
    )
    serum2_str = (
        str(plugin_paths[PresetFormat.SERUM2].resolve())
        if PresetFormat.SERUM2 in plugin_paths
        else None
    )

    results: list[dict] = []
    if deterministic:
        from .pool import iter_jobs_isolated

        result_iter = iter_jobs_isolated(
            jobs, n_workers, serum1_str, serum2_str, sample_rate
        )
    else:
        result_iter = iter_jobs(jobs, n_workers, serum1_str, serum2_str, sample_rate)

    # In verbose mode, per-preset DEBUG logs replace the progress bar so
    # the two don't fight for the terminal.
    if verbose:
        typer.echo(f"Rendering {len(jobs)} preset(s) with {n_workers} workers…")
        results = list(result_iter)
    else:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            TextColumn("•"),
            TimeRemainingColumn(),
        ) as progress:
            task_id = progress.add_task(
                f"Rendering ({n_workers} workers)", total=len(jobs)
            )
            for result in result_iter:
                results.append(result)
                progress.advance(task_id)

    ok = sum(1 for r in results if r["status"] == "ok")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    errors = [r for r in results if r["status"] == "error"]

    typer.echo(f"Done: {ok} rendered, {skipped} skipped, {len(errors)} failed.")
    for r in errors:
        typer.echo(f"  FAIL {r.get('path')}: {r.get('error')}", err=True)
    raise typer.Exit(code=1 if errors else 0)

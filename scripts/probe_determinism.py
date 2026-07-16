"""
Determinism probe (docs/decisions.md 2026-07-16, pre-committed criterion).

Measures, per format:
  0. Cold-vs-cold ceiling — the same preset rendered twice in fully
     isolated fresh processes. If those two renders are not bit-identical,
     NO reset strategy can achieve bit-reproducibility; the ceiling
     becomes the target instead.
  1. Baseline — presets chained through one engine (production behavior),
     diffed against the cold reference. Reproduces the contamination
     measurement that motivated this work.
  2. Strategies, in cost order:
       drain   — after each render: clear_midi + 0.5s idle render
       warmup  — per job: discarded 0.1s note render before the real one
       reload  — per job: fresh RenderEngine + plugin load + warmup
                 (runs with RSS tracking — DawDreamer #1 leak risk)

The cold reference is computed ONCE and reused for every strategy.

Run from the repo root:
    .venv/bin/python scripts/probe_determinism.py \
        --serum1-dir "/Library/Audio/Presets/Xfer Records/Serum Presets/Presets" \
        --serum2-dir "/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets" \
        --out-dir ./probe_determinism --per-format 30
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_SERUM1_PLUGIN = "/Library/Audio/Plug-Ins/VST/Serum.vst"
DEFAULT_SERUM2_PLUGIN = "/Library/Audio/Plug-Ins/VST3/Serum2.vst3"
SAMPLE_RATE = 44100
NOTE, VELOCITY, DURATION, TAIL = 48, 127, 1.0, 1.0

STRATEGIES = ("baseline", "drain", "warmup", "reload")


# ---------------------------------------------------------------------------
# Child mode: render a list of presets in-process with one strategy.
# ---------------------------------------------------------------------------

def child(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", required=True,
                        choices=("cold",) + STRATEGIES)
    parser.add_argument("--format", required=True, choices=("serum1", "serum2"))
    parser.add_argument("--plugin", required=True)
    parser.add_argument("--presets-file", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--suffix", default="")
    args = parser.parse_args(argv)

    import resource
    import tempfile

    import dawdreamer as daw  # first non-stdlib import
    import numpy as np

    presets = Path(args.presets_file).read_text().splitlines()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    is_serum2 = args.format == "serum2"
    if is_serum2:
        from serum2_preset_loader import convert_preset_file
        state_path = Path(tempfile.mkdtemp(prefix="probe_")) / "state.bin"

    def build():
        engine = daw.RenderEngine(SAMPLE_RATE, 512)
        synth = engine.make_plugin_processor(
            "synth", str(Path(args.plugin).resolve())
        )
        engine.load_graph([(synth, [])])
        # init warmup (absorbs Serum 2 cold-start; harmless for Serum 1)
        synth.clear_midi()
        synth.add_midi_note(NOTE, VELOCITY, 0.0, 0.05)
        engine.render(0.1)
        return engine, synth

    def load_preset(synth, preset: str) -> None:
        if is_serum2:
            state_path.write_bytes(convert_preset_file(preset))
            synth.load_state(str(state_path))
        else:
            synth.load_preset(preset)

    def real_render(engine, synth):
        synth.clear_midi()
        synth.add_midi_note(NOTE, VELOCITY, 0.0, DURATION)
        engine.render(DURATION + TAIL)
        return engine.get_audio()

    if args.strategy != "reload":
        engine, synth = build()

    for idx, preset in enumerate(presets):
        if args.strategy == "reload":
            engine, synth = build()  # fresh engine per job (probe only!)
        load_preset(synth, preset)
        if args.strategy == "warmup":
            # discarded per-job warmup render after the preset load
            synth.clear_midi()
            synth.add_midi_note(NOTE, VELOCITY, 0.0, 0.05)
            engine.render(0.1)
        audio = real_render(engine, synth)
        np.save(out_dir / f"{idx:04d}{args.suffix}.npy", audio)
        if args.strategy == "drain":
            # drain envelope/LFO tails before the next preset loads
            synth.clear_midi()
            engine.render(0.5)
        if args.strategy == "reload":
            del engine, synth

    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({"peak_rss_mb": round(peak_rss / (1024 * 1024), 1)}))


# ---------------------------------------------------------------------------
# Parent mode: orchestrate, then report.
# ---------------------------------------------------------------------------

def _spawn(strategy, fmt, plugin, presets, out_dir, suffix="") -> dict:
    presets_file = out_dir / f"_presets_{strategy}{suffix}.txt"
    presets_file.parent.mkdir(parents=True, exist_ok=True)
    presets_file.write_text("\n".join(presets))
    result = subprocess.run(
        [
            sys.executable, __file__, "child",
            "--strategy", strategy, "--format", fmt,
            "--plugin", plugin,
            "--presets-file", str(presets_file),
            "--out-dir", str(out_dir),
            "--suffix", suffix,
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _cold_reference(fmt, plugin, presets, out_dir, jobs=4) -> None:
    """One fresh process per preset, parallelised at the subprocess level.
    Each render is still fully isolated."""
    def one(idx, preset):
        d = out_dir / f"single_{idx:04d}"
        _spawn("cold", fmt, plugin, [preset], d)
        (d / "0000.npy").rename(out_dir / f"{idx:04d}.npy")

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(one, i, p) for i, p in enumerate(presets)]
        for f in as_completed(futures):
            f.result()


def _max_abs_diff(a_path: Path, b_path: Path) -> float:
    import numpy as np

    a, b = np.load(a_path), np.load(b_path)
    if a.shape != b.shape:
        return float("inf")
    return float(np.max(np.abs(a - b)))


def _sample_presets(root: Path, pattern: str, count: int) -> list[str]:
    all_presets = sorted(str(p) for p in root.rglob(pattern))
    if len(all_presets) <= count:
        return all_presets
    step = len(all_presets) / count
    return [all_presets[int(i * step)] for i in range(count)]


def parent(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serum1-dir")
    parser.add_argument("--serum2-dir")
    parser.add_argument("--serum1-plugin", default=DEFAULT_SERUM1_PLUGIN)
    parser.add_argument("--serum2-plugin", default=DEFAULT_SERUM2_PLUGIN)
    parser.add_argument("--out-dir", default="./probe_determinism")
    parser.add_argument("--per-format", type=int, default=30)
    parser.add_argument("--ceiling-presets", type=int, default=3)
    parser.add_argument("--strategies", default=",".join(STRATEGIES))
    args = parser.parse_args(argv)

    import numpy as np  # noqa: F401  (parent only diffs; safe here)

    out_root = Path(args.out_dir)
    report: dict = {}

    for fmt, preset_dir, plugin, pattern in (
        ("serum1", args.serum1_dir, args.serum1_plugin, "*.fxp"),
        ("serum2", args.serum2_dir, args.serum2_plugin, "*.SerumPreset"),
    ):
        if preset_dir is None:
            continue
        presets = _sample_presets(Path(preset_dir), pattern, args.per_format)
        print(f"[{fmt}] {len(presets)} presets sampled")
        fmt_out = out_root / fmt
        fmt_report: dict = {"n_presets": len(presets)}

        # Step 0: cold-vs-cold ceiling.
        ceiling_presets = presets[: args.ceiling_presets]
        for run in ("a", "b"):
            d = fmt_out / f"ceiling_{run}"
            if not (d / f"{len(ceiling_presets)-1:04d}.npy").exists():
                _cold_reference(fmt, plugin, ceiling_presets, d)
        ceiling_diffs = [
            _max_abs_diff(
                fmt_out / "ceiling_a" / f"{i:04d}.npy",
                fmt_out / "ceiling_b" / f"{i:04d}.npy",
            )
            for i in range(len(ceiling_presets))
        ]
        ceiling = max(ceiling_diffs)
        fmt_report["ceiling"] = {
            "max_abs": ceiling,
            "bit_identical": ceiling == 0.0,
            "per_preset": ceiling_diffs,
        }
        print(f"[{fmt}] cold-vs-cold ceiling: max_abs={ceiling:g} "
              f"(bit-identical={ceiling == 0.0})")

        # Cold reference: computed once, reused for every strategy.
        cold_dir = fmt_out / "cold"
        if not (cold_dir / f"{len(presets)-1:04d}.npy").exists():
            print(f"[{fmt}] rendering cold reference ({len(presets)} isolated processes)…")
            _cold_reference(fmt, plugin, presets, cold_dir)

        # Strategies.
        for strategy in args.strategies.split(","):
            sdir = fmt_out / strategy
            print(f"[{fmt}] strategy {strategy}…")
            stats = _spawn(strategy, fmt, plugin, presets, sdir)
            diffs = [
                _max_abs_diff(cold_dir / f"{i:04d}.npy", sdir / f"{i:04d}.npy")
                for i in range(len(presets))
            ]
            arr = sorted(diffs)
            n = len(arr)
            fmt_report[strategy] = {
                "peak_rss_mb": stats["peak_rss_mb"],
                "bit_identical": sum(1 for d in diffs if d == 0.0),
                "within_ceiling": sum(1 for d in diffs if d <= ceiling),
                "audible_ge_0.01": sum(1 for d in diffs if d >= 0.01),
                "p50": arr[n // 2],
                "p90": arr[min(n - 1, int(n * 0.9))],
                "max": arr[-1],
            }
            r = fmt_report[strategy]
            print(
                f"[{fmt}] {strategy:9s} bit-identical {r['bit_identical']}/{n}  "
                f"within-ceiling {r['within_ceiling']}/{n}  "
                f"audible {r['audible_ge_0.01']}/{n}  "
                f"p50={r['p50']:.4g} max={r['max']:.4g}  "
                f"rss={r['peak_rss_mb']}MB"
            )
        report[fmt] = fmt_report

    report_path = out_root / "report.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        child(sys.argv[2:])
    else:
        parent(sys.argv[1:])

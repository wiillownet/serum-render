"""
Verify DawDreamer assumptions from CLAUDE.md before building serum-render.

This is a throwaway script. Run once with a working Serum install and a
preset directory. If any assumption fails, the architecture in CLAUDE.md
needs adjustment before implementation proceeds.

Three assumptions under test:
  1. load_preset() on an already-loaded graph works without rebuilding
     (two sequential renders on one engine produce distinct audio).
  2. loky BrokenProcessPool recovery — a killed worker surfaces cleanly
     (not a hang) and the executor stays usable for subsequent work.
  3. load_preset() on a nonexistent path raises a catchable Python
     exception (or at minimum does not wedge the worker).

Usage:
    python scripts/verify_dawdreamer.py \\
        --plugin "C:/Program Files/Common Files/VST2/Serum.dll" \\
        --preset-dir "D:/Documents/Xfer/Serum Presets/Presets"
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

# Silence threshold matches CLAUDE.md: -90 dBFS = 16-bit quantization floor.
SILENCE_EPS = 3.16e-5


def test_sequential_load_preset(plugin_path: str, presets: list[str], sample_rate: int) -> bool:
    """Assumption 1: load_preset() on a loaded graph updates in place."""
    import dawdreamer as daw
    import numpy as np

    print("\n--- Test 1: load_preset on already-loaded graph ---")
    engine = daw.RenderEngine(sample_rate, 512)
    synth = engine.make_plugin_processor("serum", plugin_path)
    engine.load_graph([(synth, [])])

    audios = []
    for i, preset in enumerate(presets[:2]):
        synth.load_preset(preset)
        synth.clear_midi()
        synth.add_midi_note(48, 127, 0.0, 1.0)
        engine.render(2.0)
        audio = engine.get_audio()
        peak = float(np.max(np.abs(audio)))
        print(f"  Preset {i}: {Path(preset).name}  shape={audio.shape}  peak={peak:.4f}")
        if peak < SILENCE_EPS:
            print(f"  [FAIL] Preset {i} produced silent output (peak < -90 dBFS)")
            return False
        audios.append(audio)

    if np.allclose(audios[0], audios[1]):
        print("  [FAIL] Two different presets produced identical audio — "
              "load_preset may not have taken effect")
        return False
    print("  [PASS] Two presets produced distinct non-silent audio on one engine")
    return True


def test_bad_preset_path(plugin_path: str, good_preset: str, sample_rate: int) -> bool:
    """Assumption 3: load_preset() on a missing file is recoverable."""
    import dawdreamer as daw
    import numpy as np

    print("\n--- Test 3: load_preset on nonexistent path ---")
    engine = daw.RenderEngine(sample_rate, 512)
    synth = engine.make_plugin_processor("serum", plugin_path)
    engine.load_graph([(synth, [])])

    bad_path = str(Path(plugin_path).parent / "definitely_does_not_exist.fxp")
    caught_exc = None
    returned = "<not called>"
    try:
        returned = synth.load_preset(bad_path)
    except Exception as exc:
        caught_exc = exc

    if caught_exc is not None:
        print(f"  load_preset(bad path) raised {type(caught_exc).__name__}: {caught_exc}")
    else:
        print(f"  load_preset(bad path) returned {returned!r} (no exception)")

    try:
        synth.load_preset(good_preset)
        synth.clear_midi()
        synth.add_midi_note(48, 127, 0.0, 1.0)
        engine.render(2.0)
        audio = engine.get_audio()
        peak = float(np.max(np.abs(audio)))
        print(f"  Post-bad-path render: shape={audio.shape}  peak={peak:.4f}")
    except Exception as exc:
        print(f"  [FAIL] Post-bad-path render raised {type(exc).__name__}: {exc}")
        return False

    if peak < SILENCE_EPS:
        print("  [FAIL] Engine produced silent output after bad-path load")
        return False

    if caught_exc is not None:
        print("  [PASS] bad path raised a catchable exception; engine recovered")
    elif returned is False:
        print("  [PASS] bad path returned False (workers can check return value); "
              "engine recovered")
    else:
        print(f"  [WARN] bad path did not raise and returned {returned!r}. "
              "Engine recovered, but worker.py must pre-check file existence "
              "to surface a clear error.")
    return True


# Worker functions for test 2 live in _verify_loky_worker.py because
# cloudpickle round-trips functions defined in __main__ through a private
# namespace, breaking `global` assignments shared between init_worker and
# the task function. A proper importable module fixes that.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _verify_loky_worker  # noqa: E402


def test_loky_broken_pool(plugin_path: str, presets: list[str], sample_rate: int) -> bool:
    """Assumption 2: a killed worker raises cleanly and the pool recovers."""
    from loky import get_reusable_executor

    print("\n--- Test 2: loky BrokenProcessPool recovery ---")
    executor = get_reusable_executor(
        max_workers=2,
        initializer=_verify_loky_worker.init_worker,
        initargs=(plugin_path, sample_rate),
        timeout=1800,
    )

    # Warm the pool: make sure a good render works first.
    f_warm = executor.submit(_verify_loky_worker.render_task, presets[0])
    warm = f_warm.result(timeout=120)
    print(f"  Warm render OK: pid={warm['pid']}, peak={warm['peak']:.4f}")

    # Kill a worker mid-task.
    f_kill = executor.submit(_verify_loky_worker.kill_task)
    t0 = time.time()
    try:
        f_kill.result(timeout=30)
        print(f"  [FAIL] Kill task returned normally after {time.time() - t0:.1f}s")
        executor.shutdown(kill_workers=True)
        return False
    except Exception as exc:
        elapsed = time.time() - t0
        print(f"  Kill task raised {type(exc).__name__} in {elapsed:.2f}s "
              f"(expected — simulates wedged worker)")

    # Confirm the old reference is now flagged broken (submit() raises immediately).
    try:
        executor.submit(_verify_loky_worker.render_task, presets[1])
        print("  [NOTE] Old executor still accepting submissions after crash")
    except Exception as exc:
        print(f"  Old executor rejects new submissions with {type(exc).__name__} "
              f"— expected; caller must fetch a fresh executor to recover")

    # The reusable-executor recovery pattern: call get_reusable_executor()
    # again. loky transparently respawns a fresh pool under the hood.
    executor2 = get_reusable_executor(
        max_workers=2,
        initializer=_verify_loky_worker.init_worker,
        initargs=(plugin_path, sample_rate),
        timeout=1800,
    )
    try:
        rec = executor2.submit(_verify_loky_worker.render_task, presets[1]).result(timeout=120)
        print(f"  Post-kill render OK on fresh executor: pid={rec['pid']}, peak={rec['peak']:.4f}")
    except Exception as exc:
        print(f"  [FAIL] Post-kill submission on fresh executor raised "
              f"{type(exc).__name__}: {exc}")
        executor2.shutdown(kill_workers=True)
        return False

    executor2.shutdown(kill_workers=True)
    print("  [PASS] Pool survived the crash; fresh executor picks up new work")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--plugin", required=True, help="Path to VST2 Serum.dll")
    p.add_argument("--preset-dir", required=True, help="Directory with .fxp presets")
    p.add_argument("--sample-rate", type=int, default=44100)
    args = p.parse_args()

    plugin_path = str(Path(args.plugin).resolve())
    if not Path(plugin_path).exists():
        print(f"Plugin not found: {plugin_path}", file=sys.stderr)
        return 2

    presets = sorted(str(p.resolve()) for p in Path(args.preset_dir).rglob("*.fxp"))[:4]
    if len(presets) < 2:
        print(f"Need >=2 .fxp files in {args.preset_dir}, found {len(presets)}", file=sys.stderr)
        return 2

    print(f"Plugin:  {plugin_path}")
    print(f"Presets: {len(presets)} scanned, using first two:")
    for pr in presets[:2]:
        print(f"  - {pr}")

    results: dict[str, bool] = {}
    for label, fn in [
        ("1. load_preset on loaded graph", lambda: test_sequential_load_preset(plugin_path, presets, args.sample_rate)),
        ("3. bad-path recovery",           lambda: test_bad_preset_path(plugin_path, presets[0], args.sample_rate)),
        ("2. loky BrokenProcessPool",      lambda: test_loky_broken_pool(plugin_path, presets, args.sample_rate)),
    ]:
        try:
            results[label] = fn()
        except Exception:
            traceback.print_exc()
            results[label] = False

    print("\n=== Summary ===")
    for k, v in results.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    return 0 if all(results.values()) else 1


# Guard is intentional here even though CLAUDE.md forbids it in *library*
# code: this is a CLI script. Without the guard, loky's spawned workers on
# Windows would re-execute main() on import, causing a fork bomb.
if __name__ == "__main__":
    sys.exit(main())

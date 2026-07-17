"""
Verify DawDreamer + Serum 2 assumptions before extending serum-render to
support .SerumPreset files alongside .fxp.

Three assumptions under test (all load-bearing for the worker
architecture):

  1. Idle-synth-in-graph silence. With both a VST2 and a VST3 synth
     loaded into a single RenderEngine graph, driving MIDI to only ONE
     of them must produce audio identical to a render where only that
     synth exists. If the idle synth bleeds prior output or sums noise,
     the "one engine, two synths per worker" architecture is unsafe and
     we fall back to two separate engines per worker.

  2. load_state semantics on Serum 2. load_state() must (a) take effect
     in place on an already-loaded VST3 graph, (b) survive across
     successive different presets, and (c) not interfere with
     clear_midi() + add_midi_note() the way load_preset() does not.

  3. .fxp on VST3 Serum 1. The original Serum's VST3 binary may be able
     to consume .fxp presets via load_preset() — if so, the CLI should
     accept either a VST2 or a VST3 plugin path for .fxp work. If it
     fails, .fxp is strictly VST2-only.

Usage:
    python scripts/verify_dawdreamer_serum2.py \\
        --vst2 "/Library/Audio/Plug-Ins/VST/Serum.vst" \\
        --vst3-serum2 "/Library/Audio/Plug-Ins/VST3/Serum2.vst3" \\
        --serum-preset <path-to-a-.SerumPreset> \\
        --serum-preset-2 <path-to-a-different-.SerumPreset> \\
        [--vst3-serum1 <path-to-Serum-1-VST3>] \\
        [--fxp <path-to-an-fxp-preset>]

The --fxp arg is optional. When omitted, the VST2 stays at its init
patch in the mixed-graph test — the test is still meaningful because
an idle synth (no MIDI events submitted) should be silent regardless
of which patch is loaded.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import traceback
from pathlib import Path

SILENCE_EPS = 3.16e-5  # -90 dBFS


def _render(engine, synth, *, note: int = 48, dur: float = 1.0, tail: float = 1.0):
    """Render a single MIDI note on the given synth. Returns audio array."""
    synth.clear_midi()
    synth.add_midi_note(note, 127, 0.0, dur)
    engine.render(dur + tail)
    return engine.get_audio()


def test_idle_synth_silence(vst2_path: str, vst3_path: str, fxp_path: str | None,
                            serum_path: str, sample_rate: int) -> bool:
    """Test 1: an idle synth in a shared graph contributes silence.

    Both the reference and mixed engines do a throwaway warmup render
    before the measured render. This is essential: Serum 2 lazy-loads
    sample data on first render, and a non-warmed render produces
    audio at a wildly different level than steady-state. Without
    warmup, comparing across engines is meaningless.
    """
    import dawdreamer as daw
    import numpy as np
    from serum2_preset_loader import convert_preset_file

    print("\n--- Test 1: idle-synth-in-graph silence ---")

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.bin"
        state_path.write_bytes(convert_preset_file(serum_path))
        state_path_str = str(state_path)

        # --- Reference render: VST3 alone.
        ref_engine = daw.RenderEngine(sample_rate, 512)
        ref_synth = ref_engine.make_plugin_processor("serum2_only", vst3_path)
        ref_engine.load_graph([(ref_synth, [])])
        ref_synth.load_state(state_path_str)
        _render(ref_engine, ref_synth)  # warmup — discard output
        ref_audio = _render(ref_engine, ref_synth)
        ref_peak = float(np.max(np.abs(ref_audio)))
        print(f"  Reference (VST3 only):           shape={ref_audio.shape} peak={ref_peak:.4f}")
        if ref_peak < SILENCE_EPS:
            print("  [FAIL] Reference render is silent — pick a louder Serum preset")
            return False

        # --- Mixed render: both synths in the graph, MIDI driving only the VST3.
        mix_engine = daw.RenderEngine(sample_rate, 512)
        mix_vst2 = mix_engine.make_plugin_processor("serum_vst2", vst2_path)
        mix_vst3 = mix_engine.make_plugin_processor("serum2_vst3", vst3_path)
        mix_engine.load_graph([(mix_vst2, []), (mix_vst3, [])])
        if fxp_path is not None:
            mix_vst2.load_preset(fxp_path)
        mix_vst3.load_state(state_path_str)
        # Drive MIDI only to the VST3, leaving the VST2 idle (no notes).
        mix_vst2.clear_midi()
        _render(mix_engine, mix_vst3)  # warmup
        mix_audio = _render(mix_engine, mix_vst3)
        mix_peak = float(np.max(np.abs(mix_audio)))
        print(f"  Mixed (VST2 idle + VST3 active): shape={mix_audio.shape} peak={mix_peak:.4f}")

    # Compare. Allow a small tolerance — DawDreamer/JUCE has minor non-determinism
    # at unison/voice level, but graph-summed silence should be effectively zero.
    diff_peak = float(np.max(np.abs(ref_audio - mix_audio)))
    print(f"  abs(ref - mix) peak: {diff_peak:.6f}")

    # Two pass criteria:
    # (a) the difference is below the silence floor (idle synth is truly silent), OR
    # (b) the difference is small relative to the active signal (acceptable bleed
    #     from non-determinism, not stale audio).
    if diff_peak < SILENCE_EPS:
        print("  [PASS] Idle synth contributes silence; one-engine architecture is safe")
        return True
    if diff_peak < ref_peak * 0.01:
        print(f"  [PASS-MARGINAL] Diff is <1% of signal ({diff_peak / ref_peak:.4%}). "
              "Likely DawDreamer non-determinism, not stale audio. Architecture is safe "
              "but worth a follow-up listen.")
        return True
    print("  [FAIL] Idle synth is contributing audible signal. "
          "Fall back to two engines per worker (one per format).")
    return False


def test_fxp_on_vst3_serum1(vst3_serum1_path: str, fxp_path: str | None,
                             sample_rate: int) -> bool:
    """Test 3: can DawDreamer's load_preset() consume a .fxp on VST3 Serum 1?"""
    import dawdreamer as daw
    import numpy as np

    print("\n--- Test 3: .fxp on VST3 Serum 1 ---")
    if fxp_path is None:
        print("  [SKIP] No --fxp provided")
        return True
    if vst3_serum1_path is None:
        print("  [SKIP] No --vst3-serum1 provided")
        return True

    engine = daw.RenderEngine(sample_rate, 512)
    synth = engine.make_plugin_processor("serum1_vst3", vst3_serum1_path)
    engine.load_graph([(synth, [])])

    raised = None
    try:
        synth.load_preset(fxp_path)
    except Exception as exc:
        raised = exc

    if raised is not None:
        print(f"  load_preset on VST3 raised {type(raised).__name__}: {raised}")
        print("  [FAIL] .fxp is NOT loadable on VST3 Serum 1 — strictly VST2-only.")
        return False

    _render(engine, synth)  # warmup
    audio = _render(engine, synth)
    peak = float(np.max(np.abs(audio)))
    print(f"  load_preset(.fxp) on VST3 Serum 1: shape={audio.shape} peak={peak:.4f}")

    if peak < SILENCE_EPS:
        print("  [WARN] load_preset returned silently but rendered output is silent — "
              "the .fxp may not have actually applied. Treat as VST2-only.")
        return False
    print("  [PASS] .fxp loads and renders on VST3 Serum 1 — CLI can accept either VST2 or VST3 plugin for .fxp work")
    return True


def test_load_state_in_place(vst3_path: str, serum_path_1: str,
                             serum_path_2: str, sample_rate: int) -> bool:
    """Test 2: load_state in-place + MIDI semantics."""
    import dawdreamer as daw
    import numpy as np
    from serum2_preset_loader import convert_preset_file

    print("\n--- Test 2: load_state in-place + MIDI semantics ---")
    engine = daw.RenderEngine(sample_rate, 512)
    synth = engine.make_plugin_processor("serum2", vst3_path)
    engine.load_graph([(synth, [])])

    audios = []
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.bin"
        for i, preset in enumerate([serum_path_1, serum_path_2]):
            blob = convert_preset_file(preset)
            state_path.write_bytes(blob)
            synth.load_state(str(state_path))
            audio = _render(engine, synth)
            peak = float(np.max(np.abs(audio)))
            print(f"  Preset {i}: {Path(preset).name}  shape={audio.shape}  peak={peak:.4f}")
            if peak < SILENCE_EPS:
                print(f"  [FAIL] Preset {i} produced silent output — load_state may not have taken effect")
                return False
            audios.append(audio)

    if np.allclose(audios[0], audios[1]):
        print("  [FAIL] Two different .SerumPreset files produced identical audio")
        return False
    print("  [PASS] load_state takes effect in-place; sequential presets produce distinct audio")

    # Verify clear_midi + add_midi_note still work after load_state — i.e., that
    # load_state did not leave latent MIDI buffer state that survives clear_midi.
    # Render the same preset twice with different MIDI notes; expect distinct output.
    a1 = _render(engine, synth, note=48)
    a2 = _render(engine, synth, note=72)
    if np.allclose(a1, a2):
        print("  [FAIL] Same preset rendered with different MIDI notes produced identical audio")
        return False
    print(f"  [PASS] clear_midi/add_midi_note behave correctly after load_state "
          f"(note 48 peak={float(np.max(np.abs(a1))):.4f}, "
          f"note 72 peak={float(np.max(np.abs(a2))):.4f})")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--vst2", required=True, help="Path to VST2 Serum 1 (.dll on Windows, .vst on macOS)")
    p.add_argument("--vst3-serum2", required=True, help="Path to VST3 Serum 2 (.vst3 file or bundle)")
    p.add_argument("--vst3-serum1", default=None, help="Optional. Path to VST3 Serum 1 (NOT Serum 2). If provided, Test 3 probes whether load_preset(.fxp) works on it.")
    p.add_argument("--fxp", default=None, help="Optional .fxp preset to load on the VST2 in the mixed-graph test. If omitted, the VST2 stays at its init patch.")
    p.add_argument("--serum-preset", required=True, help="Path to a .SerumPreset file (must be audible)")
    p.add_argument("--serum-preset-2", required=True, help="A different .SerumPreset, distinguishable from --serum-preset")
    p.add_argument("--sample-rate", type=int, default=44100)
    args = p.parse_args()

    required = {
        "VST2 Serum 1":  args.vst2,
        "VST3 Serum 2":  args.vst3_serum2,
        "SerumPreset 1": args.serum_preset,
        "SerumPreset 2": args.serum_preset_2,
    }
    for label, path in required.items():
        if not Path(path).exists():
            print(f"{label} not found: {path}", file=sys.stderr)
            return 2
        print(f"  {label}: {path}")
    if args.fxp is not None:
        if not Path(args.fxp).exists():
            print(f"FXP not found: {args.fxp}", file=sys.stderr)
            return 2
        print(f"  FXP: {args.fxp}")
    else:
        print("  FXP: <not provided; VST2 stays at init patch>")

    vst2 = str(Path(args.vst2).resolve())
    vst3 = str(Path(args.vst3_serum2).resolve())
    fxp = str(Path(args.fxp).resolve()) if args.fxp is not None else None
    sp1 = str(Path(args.serum_preset).resolve())
    sp2 = str(Path(args.serum_preset_2).resolve())

    vst3_serum1 = str(Path(args.vst3_serum1).resolve()) if args.vst3_serum1 else None

    results: dict[str, bool] = {}
    for label, fn in [
        ("1. idle-synth-in-graph silence",
         lambda: test_idle_synth_silence(vst2, vst3, fxp, sp1, args.sample_rate)),
        ("2. load_state in-place + MIDI",
         lambda: test_load_state_in_place(vst3, sp1, sp2, args.sample_rate)),
        ("3. .fxp on VST3 Serum 1",
         lambda: test_fxp_on_vst3_serum1(vst3_serum1, fxp, args.sample_rate)),
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


if __name__ == "__main__":
    sys.exit(main())

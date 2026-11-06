"""
Measures how long Serum 2 needs after a state load before MultiSample audio is
ready, and proves a chosen wait (docs/decisions.md 2026-11-05). Results go to
docs/multisample-wait-2026-11-05.json.

Each render is one fresh process and plugin instance running EngineHost.render,
the real render path, with a given wait. A render counts only if its audio is
bit-identical to the preset's settled reference (a long wait, N renders, all
equal). Renders of one case run together at the given worker count, so the
loads contend. Each preset is converted once and its state handed to the
renders, because seven conversions of a 150 MB preset at ~2.7 GB each would
swap the machine. For the same reason a preset over 16 MiB does not run in a
burst of itself: it alternates with --filler renders, one big render to
workers - 1 others, the mix pool.spread_big_presets aims for in a real batch.

Machine state is recorded with every burst. A burst is discarded, and the run
waits for the machine to go quiet again, if other processes averaged
FOREIGN_CORES or more during it, or grew by FOREIGN_GROWTH_MB while swap grew.
Load average cannot tell during a burst, because the burst's own workers raise
it past the quiet threshold, and swap alone cannot either: eight renders of a
large preset grow it themselves, which is contention this measures.
Exit 3: the machine did not go quiet within QUIET_TIMEOUT.

Two stimuli, because a note reaches only some regions: "note" is the default
job (note 48, velocity 127), "sweep" is a MIDI file walking the keyboard at two
velocities, so a region loaded last is reached too.

Run from the repo root:
    .venv/bin/python scripts/measure_multisample_wait.py ladder out.json \
        --preset "Factory/Piano/PN - Piano Space.SerumPreset" --workers 7,8 -n 8
    .venv/bin/python scripts/measure_multisample_wait.py proof out.json \
        --preset ... --workers 7,8 -n 20

Proof mode is the shipped path end to end: pool.iter_jobs_isolated (what
--deterministic runs), real conversion in every render, the default wait, the
presets of one pass mixed and spread as in a batch. Its references come from
the same path at the settled wait.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PLUGIN = "/Library/Audio/Plug-Ins/VST3/Serum2.vst3"
ROOT = Path("/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets")
LADDER = (0, 0.1, 0.25, 0.5, 1, 1.5, 2, 3, 5, 8)
SETTLED = 15.0
PREFIX = "MEASURE "
BIG_BYTES = 16 * 2**20  # pool._BIG_PRESET_BYTES
STIMULI = ("note", "sweep")
QUIET_LOAD = 6.0
FOREIGN_CORES = 6.0
SWAP_GROWTH_MB = 25.0
FOREIGN_GROWTH_MB = 500.0
QUIET_CHECK = 600
QUIET_TIMEOUT = 7200


def other_load() -> dict:
    """What else is running: load average, and busy processes by project."""
    rows = subprocess.run(["ps", "-eo", "%cpu,command"], capture_output=True, text=True).stdout
    busy: dict = {}
    for line in rows.splitlines()[1:]:
        cpu, _, command = line.strip().partition(" ")
        project = re.search(r"Developer/([^/ ]+)", command)
        if project and float(cpu) >= 5 and "measure_multisample_wait" not in command:
            busy[project[1]] = busy.get(project[1], 0) + 1
    return {
        "load_average_1_5_15": [round(x, 1) for x in os.getloadavg()],
        "swap_used_mb": swap_mb(),
        "busy_processes": busy,
    }


def foreign_use() -> tuple[float, float]:
    """(cores, resident MB) in use by every process that is not this harness."""
    rows = subprocess.run(["ps", "-eo", "%cpu,rss,command"], capture_output=True, text=True).stdout
    cores = memory = 0.0
    for line in rows.splitlines()[1:]:
        cpu, rss, command = line.split(None, 2)
        if "measure_multisample_wait" not in command and "serum_render.isolated" not in command:
            cores += float(cpu)
            memory += float(rss)
    return cores / 100, memory / 1024


def swap_mb() -> float:
    swap = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
    return float(re.search(r"used = ([0-9.]+)M", swap)[1])


def wait_until_quiet() -> None:
    """The quiet gate: two checks QUIET_CHECK apart, load under QUIET_LOAD at
    1 and 5 minutes, swap not grown. Exits 3 after QUIET_TIMEOUT."""
    start = time.monotonic()
    previous = swap_mb()
    while True:
        time.sleep(QUIET_CHECK)
        load, swap = os.getloadavg(), swap_mb()
        print(f"  gate: load {load[0]:.1f} {load[1]:.1f} swap {swap}", flush=True)
        if load[0] < QUIET_LOAD and load[1] < QUIET_LOAD and swap <= previous + SWAP_GROWTH_MB:
            return
        if time.monotonic() - start > QUIET_TIMEOUT:
            print("machine did not go quiet: stopping", flush=True)
            sys.exit(3)
        previous = swap


def clean_burst(log: list, *args) -> tuple[list[dict], dict]:
    return guarded(log, [str(a) for a in args[:2]], lambda: burst(*args))


def guarded(log: list, label, fn) -> tuple:
    """fn(), repeated until no outside load or swap growth spoiled it. Returns
    its result and the machine state during the kept run."""
    while True:
        start_swap, start_foreign = swap_mb(), foreign_use()[1]
        samples, done = [], threading.Event()

        def sample() -> None:
            while not done.wait(2):
                samples.append(foreign_use()[0])

        sampler = threading.Thread(target=sample)
        sampler.start()
        try:
            rows = fn()
        finally:
            done.set()
            sampler.join()
        state = {
            **other_load(),
            "foreign_cores_mean": round(sum(samples) / len(samples), 2) if samples else None,
            "foreign_cores_max": round(max(samples), 2) if samples else None,
            "foreign_growth_mb": round(foreign_use()[1] - start_foreign, 1),
            "swap_growth_mb": round(swap_mb() - start_swap, 1),
        }
        spoiled = (state["foreign_cores_mean"] or 0) >= FOREIGN_CORES or (
            state["foreign_growth_mb"] > FOREIGN_GROWTH_MB and state["swap_growth_mb"] > SWAP_GROWTH_MB)
        if not spoiled:
            return rows, state
        log.append({"burst": label, "machine": state})
        print(f"  discarded burst, waiting for quiet: {state}", flush=True)
        wait_until_quiet()


def child(preset: str, wait: float, midi: str, sample_rate: int, state: str) -> None:
    import dawdreamer  # noqa: F401  must be the first non-stdlib import
    import serum2_preset_loader as loader

    from serum_render.engine import EngineHost
    from serum_render.discover import get_midi_duration
    from serum_render.formats import PresetFormat
    from serum_render.jobs import Job

    # The real render path, with the state converted once by the parent.
    loader.convert_preset_file = lambda path, keep_mpe=False: Path(state).read_bytes()
    loader.decode_preset_file = lambda path: ({}, {"Oscillator0": {"plainParams": {"kParamType": "kOsc_MultiSample"}}})
    extra = {"midi_path": midi, "midi_duration": get_midi_duration(Path(midi))} if midi else {}
    job = Job(preset, PresetFormat.SERUM2, multisample_wait=wait, **extra)
    host = EngineHost(None, PLUGIN, sample_rate)
    start = time.monotonic()
    audio, _ = host.render(job)
    print(PREFIX + json.dumps({
        "audio": hashlib.sha256(audio.tobytes()).hexdigest()[:16],
        "peak": float(abs(audio).max()),
        "render_seconds": round(time.monotonic() - start, 3),
    }), flush=True)


def write_sweep(path: Path) -> None:
    """Notes 24 to 96 at velocity 127, then at 40, one every 20 ms."""
    import mido

    mid = mido.MidiFile()
    track = mido.MidiTrack()
    mid.tracks.append(track)
    gap = int(mido.second2tick(0.02, mid.ticks_per_beat, 500000))
    for velocity in (127, 40):
        for note in range(24, 97):
            track.append(mido.Message("note_on", note=note, velocity=velocity, time=0))
            track.append(mido.Message("note_off", note=note, velocity=0, time=gap))
    mid.save(path)


def burst(preset: Path, wait: float, midi: str, sample_rate: int, n: int, workers: int, state: Path,
          filler: tuple[Path, Path] | None = None) -> list[dict]:
    def one(target: tuple[Path, Path]) -> dict:
        r = subprocess.run(
            [sys.executable, __file__, "--child", str(target[0]), str(wait), midi, str(sample_rate), str(target[1])],
            capture_output=True, text=True, timeout=900,
        )
        lines = [s for s in r.stdout.splitlines() if s.startswith(PREFIX)]
        if not lines:
            raise RuntimeError(f"render failed (exit {r.returncode}): {r.stderr[-400:]}")
        return json.loads(lines[0][len(PREFIX):])

    # Fillers only make load: their audio is not counted.
    targets = []
    for _ in range(n):
        targets.append((preset, state))
        if filler:
            targets.extend([filler] * (workers - 1))
    with ThreadPoolExecutor(workers) as pool:
        rows = list(pool.map(one, targets))
    return rows[::workers] if filler else rows


def reference(preset: Path, midi: str, sample_rate: int, state: Path, log: list) -> dict:
    """Settled reference: every render at SETTLED equal, or the preset fails."""
    rows, machine = clean_burst(log, preset, SETTLED, midi, sample_rate, 4, 4, state)
    hashes = {r["audio"] for r in rows}
    return {"audio": rows[0]["audio"] if len(hashes) == 1 else None, "peak": rows[0]["peak"],
            "renders": len(rows), "distinct": len(hashes), "machine": machine}


def needed(by_wait: list[dict], n: int) -> float | None:
    """Smallest wait from which every longer wait in the ladder was also n of n."""
    best = None
    for row in reversed(by_wait):
        if row["equal_to_reference"] != n:
            break
        best = row["wait"]
    return best


def run_cases(args, waits: tuple[float, ...]) -> dict:
    out = {"sample_rate": args.sample_rate, "renders_per_case": args.n, "settled_wait": SETTLED, "discarded_bursts": [], "presets": {}}
    from serum2_preset_loader import convert_preset_file

    tmp = Path(tempfile.mkdtemp(prefix="msw_"))
    sweep = tmp / "sweep.mid"
    write_sweep(sweep)
    for rel in args.preset:
        path = ROOT / rel
        case = out["presets"][rel] = {"size_bytes": path.stat().st_size, "stimuli": {}}
        state = tmp / "state.bin"
        state.write_bytes(convert_preset_file(str(path)))
        mixed = None
        if case["size_bytes"] > BIG_BYTES:
            if args.filler is None:
                raise SystemExit(f"{rel} is over 16 MiB: give --filler")
            filler_state = tmp / "filler.bin"
            filler_state.write_bytes(convert_preset_file(str(ROOT / args.filler)))
            mixed = (ROOT / args.filler, filler_state)
            case["filler"] = args.filler
        for stimulus in args.stimuli:
            midi = str(sweep) if stimulus == "sweep" else ""
            ref = reference(path, midi, args.sample_rate, state, out["discarded_bursts"])
            rec = case["stimuli"][stimulus] = {"reference": ref, "workers": {}}
            print(f"{rel} {stimulus}: reference {ref}", flush=True)
            if ref["audio"] is None:
                continue
            for workers in args.workers:
                rows = []
                for wait in waits:
                    t = time.monotonic()
                    res, machine = clean_burst(out["discarded_bursts"], path, wait, midi, args.sample_rate, args.n, workers, state, mixed)
                    rows.append({
                        "wait": wait,
                        "equal_to_reference": sum(r["audio"] == ref["audio"] for r in res),
                        "distinct": len({r["audio"] for r in res}),
                        "silent": sum(r["peak"] == 0 for r in res),
                        "max_render_seconds": max(r["render_seconds"] for r in res),
                        "burst_seconds": round(time.monotonic() - t, 1),
                        "machine": machine,
                    })
                    print(f"  w{workers} wait {wait}: {rows[-1]}", flush=True)
                rec["workers"][str(workers)] = {"by_wait": rows, "needed": needed(rows, args.n)}
            with open(args.out, "w") as f:
                json.dump(out, f, indent=1)
    return out


def real_pass(presets: list[str], wait: float, midi: str, sample_rate: int, n: int, workers: int) -> dict:
    """n renders of each preset through iter_jobs_isolated: {rel: [hashes]}."""
    from serum_render.discover import get_midi_duration
    from serum_render.formats import PresetFormat
    from serum_render.jobs import Job
    from serum_render.pool import iter_jobs_isolated

    extra = {"midi_path": midi, "midi_duration": get_midi_duration(Path(midi))} if midi else {}
    jobs = [Job(str(ROOT / rel), PresetFormat.SERUM2, multisample_wait=wait, **extra) for rel in presets] * n
    random.Random(0).shuffle(jobs)
    out: dict = {rel: [] for rel in presets}
    for r in iter_jobs_isolated(jobs, workers, None, PLUGIN, sample_rate, keep_audio=True):
        rel = str(Path(r["path"]).relative_to(ROOT))
        if r["status"] != "ok":
            raise RuntimeError(f"{rel}: {r.get('error')}")
        out[rel].append({"audio": hashlib.sha256(r["audio"].tobytes()).hexdigest()[:16], "peak": r["peak"]})
    return out


def run_proof(args) -> None:
    from serum_render.config import DEFAULT_MULTISAMPLE_WAIT as wait

    tmp = Path(tempfile.mkdtemp(prefix="msw_"))
    sweep = tmp / "sweep.mid"
    write_sweep(sweep)
    out = {"default_wait": wait, "sample_rate": args.sample_rate, "renders_per_case": args.n, "settled_wait": SETTLED,
           "presets": len(args.preset), "discarded_passes": [], "stimuli": {}}
    for stimulus in args.stimuli:
        midi = str(sweep) if stimulus == "sweep" else ""
        started = time.monotonic()
        refs, machine = guarded(out["discarded_passes"], [stimulus, "reference"], lambda: real_pass(
            args.preset, SETTLED, midi, args.sample_rate, 3, min(args.workers)))
        rec = out["stimuli"][stimulus] = {
            "reference_machine": machine, "reference_seconds": round(time.monotonic() - started, 1), "workers": {}}
        unsettled = {k: sorted({x["audio"] for x in v}) for k, v in refs.items() if len({x["audio"] for x in v}) != 1}
        rec["references_not_settled"] = unsettled
        for workers in args.workers:
            started = time.monotonic()
            res, machine = guarded(out["discarded_passes"], [stimulus, workers], lambda: real_pass(
                args.preset, wait, midi, args.sample_rate, args.n, workers))
            rows = {}
            for rel, got in res.items():
                ref = refs[rel][0]["audio"]
                rows[rel] = {"equal": sum(x["audio"] == ref for x in got), "of": len(got),
                             "silent": sum(x["peak"] == 0 for x in got), "reference": ref}
            failed = {k: v for k, v in rows.items() if v["equal"] != v["of"] and k not in unsettled}
            rec["workers"][str(workers)] = {
                "machine": machine, "seconds": round(time.monotonic() - started, 1),
                "presets_all_equal": sum(v["equal"] == v["of"] for v in rows.values()),
                "failed": failed, "cases": rows if args.n >= 20 else None}
            print(f"{stimulus} w{workers}: {rec['workers'][str(workers)]['presets_all_equal']}/{len(rows)} all equal, "
                  f"not settled {len(unsettled)}, failed {failed} {machine}", flush=True)
            with open(args.out, "w") as f:
                json.dump(out, f, indent=1)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=("ladder", "proof"))
    p.add_argument("out")
    p.add_argument("--preset", action="append", default=[], help="relative to the Serum 2 Presets folder")
    p.add_argument("--preset-list", help="file with one --preset value per line")
    p.add_argument("--workers", type=lambda s: [int(x) for x in s.split(",")], default=[7, 8])
    p.add_argument("-n", type=int, default=8)
    p.add_argument("--waits", type=lambda s: tuple(float(x) for x in s.split(",")), default=LADDER)
    p.add_argument("--filler", help="load for presets over 16 MiB, relative like --preset")
    p.add_argument("--sample-rate", type=int, default=44100)
    p.add_argument("--stimuli", type=lambda s: s.split(","), default=list(STIMULI))
    args = p.parse_args()
    if args.preset_list:
        args.preset += Path(args.preset_list).read_text().splitlines()
    if not args.preset:
        p.error("give --preset or --preset-list")
    if args.mode == "proof":
        run_proof(args)
    else:
        run_cases(args, args.waits)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        child(sys.argv[2], float(sys.argv[3]), sys.argv[4], int(sys.argv[5]), sys.argv[6])
    else:
        main()

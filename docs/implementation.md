# Implementation guide

Implementation context for contributors. `CLAUDE.md` holds the critical
constraints; this file holds the API contracts and the DawDreamer call
reference. The code in `serum_render/` is the source of truth for module
structure — this doc doesn't mirror skeletons.

---

## Architecture in one paragraph

`EngineHost` (`serum_render/engine.py`) is the single render core. It
builds one DawDreamer `RenderEngine` per supplied format (Serum 1 and/or
Serum 2), once, and renders typed `Job` dataclasses. The sequential
`Renderer` holds an EngineHost in the main process and lets errors
raise; the loky pool holds one per worker (module global, set by
`init_worker`) and wraps errors into `{"status": "error"}` result dicts
(`run_job`). The CLI discovers presets, resolves plugin paths (explicit
flags beat platform-default install locations), builds Jobs with output
paths, and drives `pool.iter_jobs`. There is exactly one implementation
of preset loading, MIDI setup, rendering, and the silence check.

## Worker import graph — stdlib-only at module level

Any module a worker process imports before `EngineHost.__init__` runs
must keep its module-level imports stdlib-only (plus sibling modules
under the same rule): `engine.py`, `jobs.py`, `formats.py`, `config.py`,
`output.py`. dawdreamer loads first inside `EngineHost.__init__`, then
numpy; soundfile loads inside `output.write_audio`;
`serum2_preset_loader` inside `EngineHost.render`. The package
`__init__.py` keeps `Renderer`/`ParallelRenderer`/`render_preset` behind
a lazy PEP 562 `__getattr__` for the same reason.
`tests/test_engine.py::test_engine_module_import_is_stdlib_only` pins
this in a fresh subprocess.

`discover.py` (mido at module level) and `pool.py` / `api.py` / `cli.py`
are main-process only.

## DawDreamer API reference

DawDreamer is niche — use these exact calls. Do not guess method names.

```python
import dawdreamer as daw

engine = daw.RenderEngine(44100, 512)          # (sample_rate, buffer_size)
synth = engine.make_plugin_processor("serum", str(plugin_path.resolve()))
engine.load_graph([(synth, [])])               # instruments have no inputs

synth.load_preset(str(fxp_path.resolve()))     # .fxp — absolute str only
synth.load_state(str(state_path))              # unwrapped Serum 2 state

synth.clear_midi()
synth.add_midi_note(48, 127, 0.0, 1.0)         # (note, vel, start_s, dur_s)
synth.load_midi(str(midi_path), clear_previous=True, beats=False, all_events=True)

engine.render(2.0)                             # seconds
audio = engine.get_audio()                     # (2, N) float32

import soundfile as sf
sf.write(str(out), audio.T, 44100, subtype="PCM_16")  # transpose!
```

### Serum 2 preset loading

`.SerumPreset` files are cbor2 + zstandard wrappers around the raw JUCE
state. `serum2_preset_loader.convert_preset_file(path)` returns the
inner state as bytes; `load_state` takes a path, so the bytes go through
a per-process tempfile:

```python
from serum2_preset_loader import convert_preset_file
state_path.write_bytes(convert_preset_file(str(preset_path)))
synth.load_state(str(state_path))
```

Reuse one `state_path` per process (write_bytes overwrites); never share
it across processes.

## Job contract

`Job` (`serum_render/jobs.py`) is a frozen dataclass — the typed
replacement for vst-render's documented job-dict seam. Invariants are
enforced in `__post_init__` (midi_path requires midi_duration).
`preset_path` / `midi_path` / `output_path` are absolute path strings.
`output_path=None` means "return the audio"; set means "write to disk
and return status only". `midi_duration` is computed once in the main
process (`discover.get_midi_duration`); workers never parse MIDI.

## Validation surface

1. `RenderConfig.__post_init__` — shape/range checks only, no disk I/O.
2. Renderer/ParallelRenderer entry (`api._validate_entry`) — set paths
   must exist; MIDI parsed once here.
3. `api._check_format_coverage` — every format being rendered must have
   its plugin path; error names the config field. The CLI runs its own
   equivalent in flag language (`--serum1` / `--serum2`) after default
   resolution.
4. `EngineHost.render` — one backstop RuntimeError for a format with no
   engine.

## Default plugin paths

`config.default_plugin_path(fmt, platform=None)` returns the standard
install location if it exists on disk, else None. The CLI fills defaults
only for formats present in the discovered preset set. INVARIANT: the
Serum 1 default is always a VST2 binary — the Serum 1 VST3 silently
mis-loads `.fxp`. See docs/decisions.md 2026-07-16.

## Testing

Fast unit tests (no plugin needed):

```bash
.venv/bin/pytest tests/ --ignore=tests/test_serum1_smoke.py --ignore=tests/test_serum2_smoke.py
```

Integration smokes (real Serum installs, gated per plugin):

```bash
.venv/bin/pytest tests/test_serum1_smoke.py tests/test_serum2_smoke.py \
    --serum1-plugin-path "/Library/Audio/Plug-Ins/VST/Serum.vst" \
    --serum2-plugin-path "/Library/Audio/Plug-Ins/VST3/Serum2.vst3" \
    --serum1-preset-dir  "/Library/Audio/Presets/Xfer Records/Serum Presets/Presets/Misc" \
    --serum2-preset-dir  "/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets/Factory/Piano"
```

Env-var equivalents: `SERUM1_PLUGIN_PATH`, `SERUM2_PLUGIN_PATH`,
`SERUM1_PRESET_DIR`, `SERUM2_PRESET_DIR`.

`scripts/verify_dawdreamer.py` and `scripts/verify_dawdreamer_serum2.py`
sanity-check the architectural assumptions (hot-swap, bad-path recovery,
loky crash semantics, Serum 2 state loading) against real plugins —
re-run them after upgrading DawDreamer. `scripts/probe_determinism.py`
measures the per-job reset strategies against the cold-render reference
(methodology + results in docs/decisions.md).

## Verified findings inherited from vst-render

1. `load_preset()` on a loaded graph updates in place — no graph rebuild
   between renders.
2. `load_preset()` on a missing file raises a descriptive `RuntimeError`
   and the engine stays usable.
3. A worker killed mid-batch surfaces as `TerminatedWorkerError` on its
   future within ~60 ms (no hang), but the executor reference is
   permanently broken — recovery requires a fresh
   `get_reusable_executor()` call. loky does not redistribute lost jobs.

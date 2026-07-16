# serum-render

Batch-render Serum presets to audio files. Supports Serum 1 (`.fxp`) and Serum 2 (`.SerumPreset`), on Windows and macOS, using [DawDreamer](https://github.com/DBraun/DawDreamer) as the headless engine.

One render core shared by the sequential, parallel, and CLI paths, typed jobs, stock-install plugin defaults, and a first-class answer to render reproducibility. Scope is Serum 1 + Serum 2 only, permanently.

## How it works

- Each worker process builds **one DawDreamer engine per preset format**, once. Jobs hot-swap presets on the matching engine (`load_preset` for `.fxp`; `convert → load_state` for `.SerumPreset`, decoded by [`serum2-preset-loader`](https://github.com/wiillownet/serum2-preset-loader)). The audio graph is never rebuilt mid-batch.
- A [loky](https://github.com/joblib/loky) pool fans jobs out across CPU cores; mixed-format batches dispatch per preset by file suffix.
- A warmup render at engine build absorbs Serum 2's first-render lazy-load anomaly.

## Requirements

- Windows or macOS, Python 3.11–3.12
- One or both of:
  - **Serum 1** for `.fxp` presets — the VST2 binary. macOS: `/Library/Audio/Plug-Ins/VST/Serum.vst`. Windows: `C:/Program Files/Common Files/VST3/Serum_x64.dll` (the 64-bit VST2 really does live in the VST3 folder). The VST3 build of Serum 1 will not load `.fxp` correctly.
  - **Serum 2** for `.SerumPreset` presets — the VST3. macOS: `/Library/Audio/Plug-Ins/VST3/Serum2.vst3`. Windows: `C:/Program Files/Common Files/VST3/Serum2.vst3`.
- A valid Serum license on the machine (DawDreamer does not bypass authorization).

## Install

```bash
pip install git+https://github.com/wiillownet/serum-render.git
```

## CLI

If Serum is installed in the standard location, plugin flags are optional — serum-render finds it:

```bash
serum-render "~/Documents/Serum Presets/Leads/" ./output/
```

Explicit plugin paths override the defaults:

```bash
serum-render presets/ output/ \
    --serum1 "/Library/Audio/Plug-Ins/VST/Serum.vst" \
    --serum2 "/Library/Audio/Plug-Ins/VST3/Serum2.vst3"
```

A directory containing both `.fxp` and `.SerumPreset` files renders as one mixed batch. Common options:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--serum1` | auto | Serum 1 plugin path (VST2 binary). Needed for `.fxp` input. |
| `--serum2` | auto | Serum 2 VST3 path. Needed for `.SerumPreset` input. |
| `--note` | `48` | MIDI note (0–127). Mutually exclusive with `--midi`. |
| `--velocity` | `127` | MIDI velocity (1–127). |
| `--duration` | `1.0` | Note-on duration (s). |
| `--tail` | `1.0` | Release silence after note-off (s). |
| `--sample-rate` | `44100` | Output sample rate. |
| `--bit-depth` | `16` | `16`, `24`, or `32f`. |
| `--format` | `wav` | `wav` or `npy` (raw float32 stereo array). |
| `--filename-template` | `{preset}` | Vars: `{preset}` `{note}` `{velocity}` `{folder}` `{subpath}`. |
| `--midi` | — | Render a `.mid` file instead of a single note. |
| `--workers` | `-1` | Parallel workers; `-1` = `cpu_count - 1`. |
| `--skip-existing` | off | Skip presets whose output already exists. |
| `--no-recurse` | off | Don't descend into subdirectories. |
| `--dry-run` | off | Print the render plan and exit. |

Run `serum-render --help` for the full list.

## Library API

```python
from serum_render import RenderConfig, Renderer, ParallelRenderer, render_preset

config = RenderConfig(
    serum1_plugin_path="/Library/Audio/Plug-Ins/VST/Serum.vst",
    serum2_plugin_path="/Library/Audio/Plug-Ins/VST3/Serum2.vst3",
    note=48,
    duration=1.0,
    tail=1.0,
)

# Sequential — one engine per format, reused across renders
with Renderer(config) as r:
    audio = r.render("lead.fxp")          # auto-detected as Serum 1
    audio = r.render("pad.SerumPreset")   # auto-detected as Serum 2

# Parallel mixed batch — dict of path -> (2, N) float32 array
with ParallelRenderer(config, workers=-1) as r:
    results = r.render_batch(["a.fxp", "b.fxp", "c.SerumPreset"])

# One-off
audio = render_preset("lead.fxp", config)
```

Set only the plugin paths for the formats you render; a missing path for a format actually present in the batch raises `ValueError` naming the field, before any worker boots.

## Reproducibility

By default, batch renders are **not** bit-reproducible: Serum keeps internal DSP state across consecutive renders (LFO phase, envelope residue, lazy-loaded sample data), so a preset rendered mid-batch differs from the same preset rendered alone — measured at 97% of factory presets.

`--deterministic` (or `RenderConfig(deterministic=True)`) fixes this: every preset renders in a fresh single-use process, fanned out across your worker count, making batch output **bit-identical across runs and render orders**. The cost is one plugin load per preset instead of per worker — use it when reproducibility matters (ML datasets, regression baselines), skip it when you just want samples fast.

```bash
serum-render presets/ output/ --deterministic
```

Why a whole process per preset? Serum 2 can be reset by reloading the plugin in place, but Serum 1 keeps state in library-level globals that survive even a full engine rebuild — only process isolation resets both. Probe data and methodology: `docs/decisions.md`, raw numbers in `docs/determinism-probe-2026-07-16.json`. Full caveat list: `KNOWN_ISSUES.md`.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# fast unit tests (no plugin required)
.venv/bin/pytest tests/ --ignore=tests/test_serum1_smoke.py --ignore=tests/test_serum2_smoke.py

# integration smokes (real Serum installs; each half gated independently)
.venv/bin/pytest tests/test_serum1_smoke.py tests/test_serum2_smoke.py \
    --serum1-plugin-path "/Library/Audio/Plug-Ins/VST/Serum.vst" \
    --serum2-plugin-path "/Library/Audio/Plug-Ins/VST3/Serum2.vst3" \
    --serum1-preset-dir  "$HOME/Documents/Serum Presets/Leads/" \
    --serum2-preset-dir  "$HOME/Documents/Serum 2 Presets/Pads/"
```

Env vars `SERUM1_PLUGIN_PATH`, `SERUM2_PLUGIN_PATH`, `SERUM1_PRESET_DIR`, `SERUM2_PRESET_DIR` work as flag alternatives.

## License

[GPL-3.0](LICENSE) (inherited from DawDreamer).

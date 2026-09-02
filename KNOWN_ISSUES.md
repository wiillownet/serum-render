# Known Issues

Tracked user-visible limitations and upstream quirks. Not every
limitation is a bug — some are behavioural choices documented here so
they're easy to find.

---

## Non-ASCII characters in preset paths fail to load (Windows only)

**Symptom:** a render reports `Error: (PluginProcessor::loadPreset) File not found: <path>` even though the file exists. The mangled path in the error typically shows `?` or replacement characters where the original had accented letters, CJK characters, emoji, etc.

**Cause:** DawDreamer's C++ `PluginProcessor::loadPreset` converts the Python `str` path into a narrow `std::string` via the Windows active code page; unrepresentable characters are dropped and the mangled path no longer matches the file. macOS uses UTF-8 paths end-to-end and is unaffected.

**Workaround:** rename affected presets/folders to ASCII, or pre-copy to an ASCII-safe location. serum-render handles the failure gracefully — the batch continues and these presets appear in the final error summary.

---

## Long output paths can exceed Windows `MAX_PATH` (Windows only)

serum-render caps the filename *stem* at 196 characters (headroom for `_N` collision suffixes) but does not cap the full path. Deeply nested output directories can still exceed the 260-character limit. Keep the output directory shallow or use a shorter `--filename-template`.

---

## Higher bit depths may trip the silent-output warning spuriously

The silence threshold is fixed at −90 dBFS (the 16-bit quantization floor). Legitimately quiet audio at 24-bit/32f, or presets with long attack envelopes, can trigger the `Silent output for preset` warning. The audio is still written correctly; the log line is advisory.

---

## Windows reserved filenames are not filtered (Windows only)

A preset named `CON.fxp` (or `PRN`, `NUL`, `AUX`, `COM1`–`COM9`, `LPT1`–`LPT9`) renders to a file that Windows cannot open, rename, or delete normally. `sanitize()` does not special-case reserved device names. Rename the preset, or use a template that always prefixes something (e.g. `{folder}_{preset}`).

---

## Serum 2 cold-start audio anomaly is absorbed by a warmup render

Serum 2 lazy-loads sample data on first render; the cold render comes out at ~10× steady-state level. `EngineHost` issues a 0.1-second warmup render per synth at construction, absorbing the anomaly. Do not remove the warmup — this is a regression guard, not dead code.

---

## Per-process tempfile directory is not cleaned up

Serum 2 jobs round-trip the converted state blob through a per-process tempfile (`$TMPDIR/serum_render_*/state.bin`, overwritten in place, typically <1 MB). loky doesn't run finalizers on worker exit, so directories accumulate over many runs. macOS and most Linux distros sweep `/tmp` periodically; wipe `$TMPDIR/serum_render_*` manually if needed.

---

## JUCE `attempt to map invalid URI` stderr noise on plugin load (macOS)

JUCE's plugin loader (via DawDreamer) logs a non-fatal `error: attempt to map invalid URI ...` line at worker startup. The render is unaffected. serum-render does not suppress JUCE's stderr — that would risk hiding genuine plugin errors on the same stream. Filter at the shell if needed:
`serum-render ... 2> >(grep -v "attempt to map invalid URI" >&2)`.

---

## Quarantined plugin bundles fail to load (macOS)

A plugin downloaded via browser/AirDrop/unzip carries `com.apple.quarantine`; Gatekeeper refuses the `dlopen` for unsigned or un-notarized bundles and DawDreamer surfaces the generic `RuntimeError: Unable to load plugin.` Vendor installers (including Serum's) don't set the xattr, so official installs are unaffected.

**Detect:** `xattr -lr /path/to/Plugin.vst3 | grep com.apple.quarantine`
**Fix:** `xattr -dr com.apple.quarantine /path/to/Plugin.vst3` (only for bundles from a vendor you trust), or re-run the official installer.

---

## arm64-only Python can't load x86_64-only plugins, and vice versa (macOS)

DawDreamer's PyPI wheel is single-arch; an arm64 Python can only `dlopen` arm64 (or universal2) plugins. Serum 1 and Serum 2 ship universal2 and are unaffected on current installs; this only bites with very old single-arch Serum builds. Check with:
`file "/path/to/Plugin.vst3/Contents/MacOS/Plugin"` — you want `arm64` (or both). Worst case, use a Rosetta venv (`arch -x86_64 python -m venv ...`).

---

## A worker crash mid-batch aborts the remaining jobs on that executor

loky flags the entire executor broken when any worker dies unexpectedly; every remaining job in the batch is reported as an error. Re-run with `--skip-existing` — completed outputs are skipped and only the tail re-renders.

---

## Batch renders are not bit-reproducible by default — output depends on preset order

Serum 1 and Serum 2 retain internal DSP state (LFO phase, envelope residue, lazy-loaded sample buffers) that `load_preset` / `load_state` does not fully reset, so a preset rendered mid-batch differs from the same preset rendered alone. Measured across 1491 factory presets (2026-05): 97% show audible (max_abs ≥ 0.01) warm-vs-cold variation.

serum-render addresses this with `--deterministic`, which renders every preset in a fresh single-use process — bit-identical across runs and render orders, verified against real Serum 1 + 2. See the README's reproducibility section and `docs/decisions.md` for the probe data (including why in-process resets are not enough for Serum 1).

`--deterministic` removes cross-preset state leakage, which is what the probe measured. It does **not** cover the Serum 2 multisample load race below — that one is inside a single render, so a fresh process does not help.


---

## Sample-heavy Serum 2 presets can render partially loaded (both render paths)

**Symptom:** the same preset, rendered twice with identical settings, produces
two different files. Not a small numerical drift — the two renders diverge
within the first ~5 samples and the difference can exceed the signal's own peak
amplitude. A preset that renders at peak 0.25 sometimes comes out at 0.05, and
one that renders at 0.54 sometimes comes out silent.

**Measured (2026-09-01, Serum 2, Factory/Piano):**

- 10 identical `--deterministic` renders of `PN - Piano Classic Layer`: nine at
  peak 0.2524, one at 0.0512. Exactly two outcomes, never a gradient.
- Two `--deterministic` passes over the 12-preset folder: 1–4 files differ per
  trial. The same folder under `Factory/Instrument` (3 presets, no multisamples)
  was bit-identical across every trial.
- The default warm-pool path is affected too, so this is not specific to
  `--deterministic`.

**Cause:** Serum 2 lazy-loads sample data, and for heavy multisampled
instruments that load is not always complete when the render starts. The
0.1-second warmup render in `EngineHost` absorbs the cold-start *level* anomaly
(see above) but does not guarantee a large multisample set is resident. The
render then captures a partially-loaded instrument. This is unrelated to
presets referencing missing sample files — the affected renders logged no
`can't open` warnings.

**Not fixed, and a settle render is not the fix.** Tested (2026-09-01):
inserting a throwaway render between `load_state` and the real render, swept
from 0 to 3 seconds, across five sample-based presets. Results do not
generalise — at a 1-second settle, `PN - Piano Space` and
`GTR - Acoustic Guitar Duo` became perfectly stable, `PN - Piano Classic Layer`
moved from a stable-but-wrong 0.0512 to ~0.3325 with 0.2% jitter,
`PN - Piano Dance` got *worse* (stable at 0 settle, alternating between two
values at 1 second), and `PN - Ambient Piano` was unchanged: 8 distinct peaks
across 8 loads in both conditions, ranging 0.08 to 0.30.

That last one probably is not a load race at all — a spread that wide, present
regardless of settle time, looks like per-note randomisation inside the preset
(random phase, noise, sample start). So at least two distinct effects are mixed
together here, and a single warmup knob cannot address both.

Also note the first load in a process usually differs from every later one:
with no settle, most presets tested were stable across loads 2..N but not on
load 1. That is the same order-dependence documented above, not a separate
problem.

**Detect:** render twice and compare, e.g.
`cmp a/preset.wav b/preset.wav`, or compare peak amplitudes — the failure mode
is bimodal and obvious, not subtle. Affected presets are the sample-based ones
(pianos, guitars, drum kits, orchestral); pure-synthesis presets and all of
Serum 1 are unaffected.

---

## A killed render can leave a stray `.tmp` file

`write_audio` renders to `<output>.wav.tmp` / `<output>.npy.tmp` and renames it into place, so a `SIGKILL` mid-write can never leave a truncated file at the real output path — which `--skip-existing` would otherwise skip forever on the re-run. The trade is that a killed batch leaves up to `--workers` stray `.tmp` files. The suffix goes *after* the real extension deliberately, so strays stay out of `*.wav` globs. Delete them with `find <output> -name '*.tmp' -delete`.

---

## Worker stdout is shared with the `--json` event stream

loky workers inherit the parent's stdout, so a plugin printing from C code could in principle interleave with `--json` output and split an event line in two. This has never been observed — the only noise Serum's loader is known to produce goes to stderr (see the JUCE entry above) — and nothing in serum-render writes to stdout from a worker. Consumers should skip any line that fails to parse and trust the counts in the `done` event over their own tally of `result` events, which is where a lost line becomes visible.

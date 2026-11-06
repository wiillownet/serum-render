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

## A worker crash mid-batch aborts the batch

loky flags the entire executor broken when any worker dies unexpectedly, so the remaining jobs cannot run. Since 0.4.0 the CLI reports this once (an `ABORTED` line on stderr; under `--json`, an `aborted` string on the `done` event) and exits 1 instead of listing every remaining preset as an error. loky cannot say which preset was in flight. Re-run with `--skip-existing` — completed outputs are skipped and only the tail re-renders.

---

## Batch renders are not bit-reproducible by default — output depends on preset order

Serum 1 and Serum 2 retain internal DSP state (LFO phase, envelope residue, lazy-loaded sample buffers) that `load_preset` / `load_state` does not fully reset, so a preset rendered mid-batch differs from the same preset rendered alone. Measured across 1491 factory presets (2026-05): 97% show audible (max_abs ≥ 0.01) warm-vs-cold variation. Sample-based Serum 2 presets are not exempt: a 90 MB Splice preset rendered cold three times was bit-identical, while two of three warm renders carried audio from sample 0, before the note-on (2026-09-07). Since 0.4.0 loky also recycles a worker whose memory grew past a threshold, so some presets in a default batch happen to render cold; that changes which presets vary, not the guarantee, which remains `--deterministic` only.

serum-render addresses this with `--deterministic`, which renders every preset in a fresh single-use process — bit-identical across runs and render orders, verified against real Serum 1 + 2. See the README's reproducibility section and `docs/decisions.md` for the probe data (including why in-process resets are not enough for Serum 1).

`--deterministic` removes cross-preset state leakage, which is what the probe measured. It does not cover the Serum 2 MultiSample load race below, which happens inside a single render; the wait described there covers that.


---

## MultiSample presets can render partially loaded without a wait (fixed in 0.7.1)

**Symptom, before 0.7.1:** the same Serum 2 preset, rendered with identical settings, could come out at a different level or silent. The difference is large and comes in a few discrete outcomes, not a gradient. Some presets were stable and wrong, so rendering twice and comparing did not reveal it.

**Cause:** a state load queues one task per sample on a thread pool and returns, and nothing waits for them. A note-on that arrives before a region's data is ready starts no voice for that region for that whole note. An offline render reaches its first note in milliseconds, so it loses this race often. The Serum 2 study (`re` repo, spec 10 section 17.4, Quirk Q78) showed this with one fresh instance per render and identical host calls: a wall-clock wait after the load makes repeats bit-identical, and presets without a MultiSample oscillator did not vary.

**What 0.7.1 does:** it waits between the state load and the first note, 10 seconds by default (`--multisample-wait`, `RenderConfig(multisample_wait=...)`), only when a preset has an oscillator of type MultiSample. Measured at 7 and 8 workers (`docs/multisample-wait-2026-11-05.json`):

- The longest any preset needed was 5 seconds. Presets that embed their audio needed no wait.
- At the default, the 13 largest cases gave 20 of 20 renders equal to their settled reference, for a single note and a keyboard sweep at both worker counts. All 186 installed MultiSample presets gave 3 of 3.
- Presets without MultiSample render byte-identically to 0.7.0, with no wait.

An earlier version of this entry said a settle render was not the fix. That sweep ran consecutive loads in one process, so the race was mixed with the warm-instance history described above. It also used a render, where the threads need wall-clock time.

**Limits:** the measurements are from one 8-core machine with samples in the file cache. A slower disk or a first read may need more, so raise the wait if a preset still varies. Each MultiSample preset costs a worker that long: the full 778-preset library at 7 workers went from 117 to 326 seconds. Warm-pool renders still vary with worker history, as above; `--deterministic` remains the only reproducibility guarantee. A preset can also be silent for a simple reason: a drum kit with nothing mapped to the rendered note (`KIT - Auxillary Kit` at C3) is silent at any wait.

---

## A killed render can leave a stray `.tmp` file

`write_audio` renders to `<output>.wav.tmp` / `<output>.npy.tmp` and renames it into place, so a `SIGKILL` mid-write can never leave a truncated file at the real output path — which `--skip-existing` would otherwise skip forever on the re-run. The trade is that a killed batch leaves up to `--workers` stray `.tmp` files. The suffix goes *after* the real extension deliberately, so strays stay out of `*.wav` globs. Delete them with `find <output> -name '*.tmp' -delete`.

---

## Worker stdout is shared with the `--json` event stream

loky workers inherit the parent's stdout, so a plugin printing from C code could in principle interleave with `--json` output and split an event line in two. This has never been observed — the only noise Serum's loader is known to produce goes to stderr (see the JUCE entry above) — and nothing in serum-render writes to stdout from a worker. Consumers should skip any line that fails to parse and trust the counts in the `done` event over their own tally of `result` events, which is where a lost line becomes visible.

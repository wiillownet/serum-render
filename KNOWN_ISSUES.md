# Known Issues

Tracked user-visible limitations and upstream quirks, carried over from
vst-render where still applicable. Not every limitation is a bug — some
are behavioural choices documented here so they're easy to find.

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

## `serum2-preset-loader` is pinned to a git commit, not a PyPI release

`pyproject.toml` pins it via `git+https://...@<40-char-sha>` and sets `[tool.hatch.metadata] allow-direct-references = true`. Downstream packagers re-declaring this dependency need the same opt-in. The pin moves to a PyPI version once upstream ships one.

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

Serum 1 and Serum 2 retain internal DSP state (LFO phase, envelope residue, lazy-loaded sample buffers) that `load_preset` / `load_state` does not fully reset, so a preset rendered mid-batch differs from the same preset rendered alone. Measured in vst-render across 1491 factory presets: 97% show audible (max_abs ≥ 0.01) warm-vs-cold variation.

serum-render addresses this with a per-job reset mode — see the determinism section in the README for the flag, the strategy chosen per format, and the measured results (`docs/decisions.md` has the probe data).

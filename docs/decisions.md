# Decisions

The *why* behind non-trivial choices, so they aren't silently re-litigated.
Append entries; don't rewrite history.

## [2026-07-16] Ground-up rewrite rather than refactor of the predecessor

**Decision:** Rebuild from scratch in a fresh repo rather than refactor the predecessor library in place.

**Reason:** The predecessor's render path was implemented twice (an in-process module and a separate loky worker module), the CLI bypassed the public API through an untyped 14-key job-dict "public seam", plugin-path validation was spread across four layers, mutable config forced a freeze-on-enter hack, and its engine dataclass carried five nullable fields. The rebuild keeps all verified DawDreamer knowledge (import order, no threading, engine-per-format, warmup render, absolute paths, loky crash semantics) and the full unit-test suite, but collapses everything onto one render core (`EngineHost`) shared by sequential, parallel, and CLI paths.

**Alternatives considered:** Incremental refactor — rejected because the duplication was structural (two entry stacks with different import-order contracts) and every intermediate state would need both stacks kept green.

## [2026-07-16] Scope is Serum 1 + Serum 2 only, permanently

**Decision:** Support `.fxp` (Serum 1) and `.SerumPreset` (Serum 2) only. No Vital, no generic `.vstpreset`, ever.

**Reason:** Owner's explicit call: Vital support was abandoned and will not return. Hardcoding two formats removes the extensibility machinery (format registry as public seam, per-format flag plumbing) that helped convolute the predecessor.

## [2026-07-16] Determinism exit criterion (pre-committed before probing)

**Decision:** `scripts/probe_determinism.py` measures the cold-vs-cold ceiling per format FIRST (same preset, two fully isolated fresh processes). If a per-job reset strategy achieves bit-identity — or matches the measured ceiling — it ships as `--deterministic`. If no strategy reaches the ceiling, the best strategy ships under the mechanism-honest name `--reset-per-job` instead, and the README states the measured residual. Strategy may differ per format. Candidate strategies in cost order: (a) all-notes-off + idle drain render, (b) discarded per-job warmup render, (c) full synth reload per job (always paired with a warmup render, and probed over a long batch with RSS monitoring — DawDreamer issue #1 leak risk).

**Reason:** A 1491-preset sweep (2026-05) measured that 97% of presets render audibly differently warm vs cold (state contamination: LFO phase, envelope residue, lazy-loaded sample data). Committing the exit criterion before running the probe prevents shipping a flag whose name overpromises. Bit-reproducibility may be structurally unreachable if Serum seeds LFO phase / analog drift from an RNG or clock — the ceiling measurement detects that case up front.

## [2026-07-16] Deterministic mode ships as fresh-process-per-preset (probe results)

**Decision:** `--deterministic` / `RenderConfig.deterministic` renders every preset in a fresh single-use process (`serum_render/isolated.py`), fanned out across the worker count. Both formats use the same mechanism. The flag keeps the name `--deterministic` because the pre-committed criterion was met: output is bit-identical across runs.

**Reason:** Probe results (`scripts/probe_determinism.py`, 30 presets/format sampled across the factory libraries, raw report in `docs/determinism-probe-2026-07-16.json`):

| | ceiling (cold-vs-cold) | baseline | drain | warmup | reload (in-process) |
|---|---|---|---|---|---|
| Serum 1 | bit-identical | 1/30 bit-identical | 1/30 | 0/30 | 4/30 |
| Serum 2 | bit-identical | 1/30 bit-identical | 1/30 | 0/30 | **30/30 bit-identical** |

Three findings drove the design:
1. The cold-vs-cold ceiling is bit-identical for both formats, so bit-reproducibility is achievable — Serum does not seed LFO phase or drift from an RNG/clock across process launches.
2. Serum 2 is fully reset by an in-process engine + plugin reload (30/30).
3. Serum 1 is NOT: even a full `RenderEngine` + `make_plugin_processor` rebuild in the same process leaves 26/30 presets audibly contaminated. Serum 1 keeps state in dylib-level globals that survive plugin re-instantiation; only process death resets it.

Fresh-process-per-preset is the one mechanism that works for both formats, is exactly the isolation the ceiling measured, keeps a single code path, and sidesteps the documented engine-per-job anti-pattern (DawDreamer issues #88/#1) inside long-lived workers. Verified end-to-end: a mixed batch rendered twice in different orders is bit-identical per preset (`test_deterministic_batches_are_bit_identical`), and two full CLI runs produce `cmp`-identical WAVs.

**Cost:** one interpreter start + plugin load per preset (~2–4s overhead each, parallelised across `--workers`) instead of one per worker. The default fast path is unchanged.

**Alternatives considered:** per-format split (in-process reload for Serum 2, subprocess only for Serum 1) — rejected: two code paths to keep in sync for a modest Serum 2 speedup, and in-worker engine rebuilding is the pattern upstream warns leaks. Drain / per-job-warmup strategies — rejected: measured ineffective (≤1/30).

## [2026-07-16] Default plugin paths, resolved per discovered format

**Decision:** When `--serum1` / `--serum2` is omitted, fall back to the standard install location — but only for formats actually present in the discovered preset set, and only if the default path exists on disk. A missing default is treated as "unset" (normal missing-plugin error naming the flag), never an error by itself. Explicit flag always beats the default. Invariant: the Serum 1 default is always a VST2 binary (macOS `/Library/Audio/Plug-Ins/VST/Serum.vst`, Windows `C:/Program Files/Common Files/VST3/Serum_x64.dll`) — never the Serum 1 VST3, which silently mis-loads `.fxp`.

**Reason:** Serum-only-forever scope makes stock-install defaults safe and a big UX win (`serum-render presets/ out/` just works). Filling only discovered formats avoids booting an unused synth (wasted init, JUCE stderr noise, per-job reset cost in deterministic mode).

## [2026-09-01] No settle render for the Serum 2 multisample instability

**Decision:** Leave the render path alone. Sample-based Serum 2 presets can render at the wrong level or unstably (see `KNOWN_ISSUES.md`); a throwaway "settle" render between `load_state` and the real render is **not** the fix and is not shipping. The 0.1s warmup at `EngineHost` construction stays exactly as it is.

**Reason:** Swept a post-load settle render from 0 to 3 seconds across five sample-based Factory presets, 8 consecutive loads each, recording peak amplitude:

| preset | settle=0 | settle=1.0s |
|---|---|---|
| PN - Piano Classic Layer | stable 0.0512 (wrong level) | ~0.3325, 0.2% jitter |
| PN - Piano Space | 2 distinct | **1 distinct — stable** |
| GTR - Acoustic Guitar Duo | 2 distinct | **1 distinct — stable** |
| PN - Piano Dance | stable 0.0327 | **2 distinct — worse** |
| PN - Ambient Piano | 8 distinct (0.08–0.30) | 8 distinct (0.10–0.30) |

It fixes two presets, regresses one, and does nothing for a fourth. Shipping it would trade a documented problem for an undocumented one that varies per preset.

Two effects are mixed together here, which is why one knob cannot address them:

1. **A load-completeness effect.** `Piano Classic Layer` renders a *stable but wrong* 0.0512 with no settle and converges to ~0.333 with one; settle values of 1.0, 2.0 and 3.0 give byte-identical results, so it does plateau. Stable-but-wrong is the nastier failure — re-rendering and comparing, the detection advice in `KNOWN_ISSUES.md`, will not catch it.
2. **Per-note randomisation inside the preset.** `Ambient Piano` gives 8 distinct peaks across 8 loads regardless of settle time. A spread that wide and that settle-independent reads as random phase / noise / sample-start in the preset itself, which no host-side warmup can remove.

Also measured: with no settle, most presets are stable across loads 2..N and only load 1 differs. That is the order-dependence already documented, not a third effect.

**Alternatives considered:** a longer warmup at `EngineHost` construction — rejected, it happens before any preset is loaded, so it cannot affect a per-preset sample load. A per-preset settle tuned by sample count or blob size — not attempted: worth doing only if a structural trait is first shown to predict which presets drift, and that has not been established. Waiting on a load-complete signal from the plugin — no such signal is known to be exposed via DawDreamer.

## [2026-09-01] A GUI drives serum-render as a subprocess over `--json`, not as a library

**Decision:** the forthcoming GUI (separate repo, PySide6) spawns `python -m serum_render ... --json` and parses an NDJSON event stream from stdout. serum-render gains `--json`, `__main__.py`, an atomic `write_audio`, and a `peak` field on the result dict. It does **not** gain a public batch API for the GUI to call.

**Reason:** the GUI must not host the render, because DawDreamer hangs off the main thread (`engine.py`) — that is settled. What was not obvious is which *kind* of process boundary. The deciding factor is packaging: the GUI is intended to ship as a bundled app, and a frozen PySide6 app must never host loky's workers, because `spawn` re-executes `sys.executable`, which inside a bundle is the app itself. Spawning a separate interpreter sidesteps that entirely. Secondarily, `--json` is useful for scripting independent of any GUI, and the subprocess reuses the ~110 lines of validation, discovery, filename composition and collision resolution in `cli.py` rather than duplicating them.

Two reasons that were considered and are explicitly **not** why, recorded so they are not repeated as justification: *crash isolation* (the parent process is identical either way — dawdreamer only loads inside `EngineHost.__init__`, in a worker — so a segfault kills a worker in both designs, and no worker death has ever been observed here), and *cleaner cancellation* (loky's `shutdown(wait=True, kill_workers=True)` is one line, against the ~40 lines of process-group code the subprocess boundary requires).

**Alternatives considered:** extract `cli.py`'s job-building into a public `plan_jobs() -> list[Job]` and drive `pool.iter_jobs` from a GUI background thread — genuinely works (the pool parent never imports dawdreamer, and `iter_jobs_isolated` already threads the parent), and is roughly 200 lines cheaper overall, but forecloses the bundled-app path above. A GUI-owned driver subprocess emitting its own protocol — pays the protocol cost *and* duplicates `cli.py`. Rejected: putting the event stream on stderr, which would delete the stdout-hygiene work but move events onto the one stream with observed C-level writes (JUCE, see `KNOWN_ISSUES.md`). Rejected: an `os.dup2(2, 1)` worker initializer to pre-empt stdout pollution — the hazard is structural but has never been observed, and placing it in `init_worker` breaks every `--deterministic` render, since `isolated.py` calls that same function and then prints its result line to stdout.

**Contract the GUI depends on, recorded because it lives nowhere else:** exit `0` clean, `1` some renders failed, `2` usage or validation error. Validation errors produce no events, so exit 0 always implies at least one `done` event — including the no-presets case, which emits `start` with `total: 0`. Shape is unstable until 1.0; the `schema` key exists so a consumer fails loudly instead of mis-parsing.

**Reversal criterion:** revisit if the GUI ever needs per-job *start* events (the parent submits all futures at once and cannot know when a job begins, so that needs a worker-side change) or the audio array itself.

## [2026-09-05] Render what the installed plugins cover, behind a flag

**Decision:** Add `--skip-missing-format`, default off. With it, a batch whose library holds a format with no installed plugin renders the presets it can and reports the rest as ordinary `skipped` results carrying `"reason": "no_plugin"`; without it, `cli.py`'s existing refusal of the whole batch with exit 2 is unchanged. Skips from `--skip-existing` gained the matching `"reason": "exists"`. `start`'s `total` counts dropped presets, matching how skip-existing skips were already counted inside it. If *nothing* is renderable, the flag still exits 2.

**Reason:** A library holding both `.fxp` and `.SerumPreset` on a machine with one synth installed was entirely unrenderable — and that is the common install, not an edge case. The GUI's design makes filtering a hard requirement: it states the split ("770 will render · 765 need Serum 2, skipped") in the footer and puts the renderable count on the Render button, so it cannot call a code path that aborts instead. Default-off keeps the failure loud where it should be: a typo'd `--serum2` path must not quietly render half a library, and the flag is opt-in precisely so that mistake still costs an exit 2. `reason` rather than a new status value means `done`'s `skipped` count already includes these and an existing consumer needs no new branch — while the GUI, which must word the two skips differently ("never renderable" vs "already rendered"), can tell them apart.

**Alternatives considered:** `--only-format serum1|serum2`, with the caller running one batch per installed synth — two plugin cold starts at ~10–15s each and two event streams to merge, to express what one flag says. Passing an explicit preset list instead of a directory — the CLI's whole input contract is one path, so this is a larger change than the problem. Making the filtering behaviour the default — rejected because it silently converts a misconfiguration into a half-finished batch. A distinct `"status": "unrenderable"` — rejected because every existing consumer would need a new branch to keep its counts reconciling.

**Note:** the GUI plan (revision 3) explicitly said "do not add a `--skip-missing-format` flag on speculation." That was correct on the information then available; the speculation resolved when the design made batch filtering a requirement rather than a guess.

## [2026-09-05] Expected plugin suffix lives beside the default plugin paths

**Decision:** Add `_PLUGIN_SUFFIXES` to `config.py` alongside `_DEFAULT_PLUGIN_PATHS`, with `plugin_suffix_for(fmt, platform)` and `plugin_path_looks_valid(path, fmt, platform)`. Serum 1 (VST2) expects `.vst` on macOS, `.dll` on Windows, `.so` on Linux; Serum 2 (VST3) expects `.vst3` everywhere. Validity is `exists()` plus a case-folded suffix match; an unknown platform enforces existence only.

**Reason:** A user browsing for a plugin by hand can pick the folder above the bundle, or hand the Serum 2 row a Serum 1 `.vst` — and the Serum 1 VST3 in particular *silently mis-loads* `.fxp` presets rather than failing, which is the exact failure this check exists to catch before a 1500-preset batch runs. Putting the table next to `_DEFAULT_PLUGIN_PATHS` means the check and the default read the same platform knowledge and cannot drift apart, which is why this lives in serum-render rather than in the GUI that needs it.

**Alternatives considered:** Implementing the check GUI-side — rejected because the two tables would then encode the same platform facts in two repos. Using `is_file()` — actively wrong: both plugin formats are *bundles* (directories) on macOS, so `is_file()` rejects every valid macOS plugin.

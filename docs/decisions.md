# Decisions

The *why* behind non-trivial choices, so they aren't silently re-litigated.
Append entries; don't rewrite history.

## [2026-07-16] Ground-up rebuild of vst-render as serum-render

**Decision:** Rebuild vst-render from scratch in this repo rather than refactor in place.

**Reason:** vst-render's render path was implemented twice (`renderer.py` in-process vs `worker.py` for loky), the CLI bypassed the public API through an untyped 14-key job-dict "public seam", plugin-path validation was spread across four layers, mutable config forced a freeze-on-enter hack, and the Engine dataclass carried five nullable fields. The rebuild keeps all verified DawDreamer knowledge (import order, no threading, engine-per-format, warmup render, absolute paths, loky crash semantics) and the full unit-test suite, but collapses everything onto one render core (`EngineHost`) shared by sequential, parallel, and CLI paths.

**Alternatives considered:** Incremental refactor of vst-render — rejected because the duplication is structural (two entry stacks with different import-order contracts) and every intermediate state would need both stacks kept green.

## [2026-07-16] Scope is Serum 1 + Serum 2 only, permanently

**Decision:** Support `.fxp` (Serum 1) and `.SerumPreset` (Serum 2) only. No Vital, no generic `.vstpreset`, ever.

**Reason:** Owner's explicit call: Vital support was abandoned and will not return. Hardcoding two formats removes the extensibility machinery (format registry as public seam, per-format flag plumbing) that helped convolute vst-render.

## [2026-07-16] Determinism exit criterion (pre-committed before probing)

**Decision:** `scripts/probe_determinism.py` measures the cold-vs-cold ceiling per format FIRST (same preset, two fully isolated fresh processes). If a per-job reset strategy achieves bit-identity — or matches the measured ceiling — it ships as `--deterministic`. If no strategy reaches the ceiling, the best strategy ships under the mechanism-honest name `--reset-per-job` instead, and the README states the measured residual. Strategy may differ per format. Candidate strategies in cost order: (a) all-notes-off + idle drain render, (b) discarded per-job warmup render, (c) full synth reload per job (always paired with a warmup render, and probed over a long batch with RSS monitoring — DawDreamer issue #1 leak risk).

**Reason:** vst-render measured that 97% of presets render audibly differently warm vs cold (state contamination: LFO phase, envelope residue, lazy-loaded sample data). Committing the exit criterion before running the probe prevents shipping a flag whose name overpromises. Bit-reproducibility may be structurally unreachable if Serum seeds LFO phase / analog drift from an RNG or clock — the ceiling measurement detects that case up front.

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

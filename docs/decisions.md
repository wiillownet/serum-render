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

## [2026-07-16] Default plugin paths, resolved per discovered format

**Decision:** When `--serum1` / `--serum2` is omitted, fall back to the standard install location — but only for formats actually present in the discovered preset set, and only if the default path exists on disk. A missing default is treated as "unset" (normal missing-plugin error naming the flag), never an error by itself. Explicit flag always beats the default. Invariant: the Serum 1 default is always a VST2 binary (macOS `/Library/Audio/Plug-Ins/VST/Serum.vst`, Windows `C:/Program Files/Common Files/VST3/Serum_x64.dll`) — never the Serum 1 VST3, which silently mis-loads `.fxp`.

**Reason:** Serum-only-forever scope makes stock-install defaults safe and a big UX win (`serum-render presets/ out/` just works). Filling only discovered formats avoids booting an unused synth (wasted init, JUCE stderr noise, per-job reset cost in deterministic mode).

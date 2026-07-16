"""
Single-use render process for deterministic mode.

The 2026-07-16 determinism probe (docs/decisions.md) showed that Serum 1
retains state across even a full in-process engine + plugin reload —
contamination lives in dylib-level globals that only die with the
process. Process isolation is also what the cold-vs-cold ceiling
measured as bit-identical, so deterministic mode renders every preset in
a fresh single-use process for both formats.

Child entry point:
    python -m serum_render.isolated <payload.json>

Payload schema (written by pool.render_isolated):
    {
      "serum1_plugin_path": str | null,
      "serum2_plugin_path": str | null,
      "sample_rate": int,
      "job": {<Job fields, format as its string value>},
      "audio_out": str | null   # .npy path when the caller wants audio back
    }

The child prints one result line prefixed with RESULT_PREFIX so the
parent can find it amid plugin-loader stdout noise. Module level is
stdlib-only (this module IS the render process).
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

RESULT_PREFIX = "SERUM_RENDER_RESULT "


def job_to_payload(
    job,
    serum1_plugin_path: str | None,
    serum2_plugin_path: str | None,
    sample_rate: int,
    audio_out: str | None,
) -> dict:
    return {
        "serum1_plugin_path": serum1_plugin_path,
        "serum2_plugin_path": serum2_plugin_path,
        "sample_rate": sample_rate,
        "job": dataclasses.asdict(job),
        "audio_out": audio_out,
    }


def parse_result_line(stdout: str) -> dict | None:
    """Find the child's result line in its stdout (plugin loaders print
    their own noise to the same stream)."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            return json.loads(line[len(RESULT_PREFIX):])
    return None


def main(argv: list[str]) -> int:
    payload = json.loads(Path(argv[0]).read_text())

    from .engine import init_worker, run_job
    from .jobs import Job

    # Build only the engine this job's format needs — a single-use
    # process has no reason to boot the other plugin.
    job = Job(**payload["job"])
    fmt = str(job.format)
    init_worker(
        payload["serum1_plugin_path"] if fmt == "serum1" else None,
        payload["serum2_plugin_path"] if fmt == "serum2" else None,
        payload["sample_rate"],
    )
    result = run_job(job)

    audio = result.pop("audio", None)
    if audio is not None and payload["audio_out"] is not None:
        import numpy as np

        np.save(payload["audio_out"], audio)
        result["audio_out"] = payload["audio_out"]

    print(RESULT_PREFIX + json.dumps(result), flush=True)
    # Render errors travel in-band as {"status": "error"} result lines;
    # a nonzero exit is reserved for the process itself blowing up.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

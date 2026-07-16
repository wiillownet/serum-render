"""loky executor management + batch dispatch. Main-process only —
workers import serum_render.engine, never this module."""
from __future__ import annotations

import logging
import os
from concurrent.futures import as_completed
from pathlib import Path
from typing import Iterator

from loky import get_reusable_executor

from .engine import init_worker, run_job
from .jobs import Job

logger = logging.getLogger("serum_render")


def resolve_worker_count(workers: int) -> int:
    """-1 -> cpu_count - 1 (floor 1); otherwise max(1, workers)."""
    if workers == -1:
        return max(1, (os.cpu_count() or 2) - 1)
    return max(1, workers)


def iter_jobs(
    jobs: list[Job],
    workers: int,
    serum1_plugin_path: str | None,
    serum2_plugin_path: str | None,
    sample_rate: int,
) -> Iterator[dict]:
    """
    Submit every job to the reusable pool and yield result dicts as they
    complete (unordered — driven by whichever worker finishes first).

    The 30-minute idle timeout keeps workers warm for long-running
    embedders; the executor is a process-wide singleton owned by loky.

    If a worker process crashes, loky permanently flags the executor
    broken — every remaining future raises and is surfaced here as an
    error result. Re-running with skip_existing=True is idempotent for
    the jobs that already landed on disk.
    """
    executor = get_reusable_executor(
        max_workers=resolve_worker_count(workers),
        initializer=init_worker,
        initargs=(serum1_plugin_path, serum2_plugin_path, sample_rate),
        timeout=1800,
    )
    futures = {executor.submit(run_job, job): job for job in jobs}
    for future in as_completed(futures):
        job = futures[future]
        try:
            yield future.result()
        except Exception as exc:
            logger.error("Worker error for %s: %s", job.preset_path, exc)
            yield {"status": "error", "path": job.preset_path, "error": str(exc)}


def render_isolated(
    job: Job,
    serum1_plugin_path: str | None,
    serum2_plugin_path: str | None,
    sample_rate: int,
    keep_audio: bool,
) -> dict:
    """Render one Job in a fresh single-use subprocess (deterministic
    mode). Returns the same result-dict shape as run_job; when
    `keep_audio` is set the child round-trips the array through a
    tempfile and it's loaded back here."""
    import json
    import subprocess
    import sys
    import tempfile

    from .isolated import job_to_payload, parse_result_line

    with tempfile.TemporaryDirectory(prefix="serum_render_iso_") as tmpdir:
        audio_out = str(Path(tmpdir) / "audio.npy") if keep_audio else None
        payload_path = Path(tmpdir) / "payload.json"
        payload_path.write_text(
            json.dumps(
                job_to_payload(
                    job, serum1_plugin_path, serum2_plugin_path,
                    sample_rate, audio_out,
                )
            )
        )
        proc = subprocess.run(
            [sys.executable, "-m", "serum_render.isolated", str(payload_path)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            tail = "\n".join(proc.stderr.splitlines()[-5:])
            logger.error("Isolated render died for %s: %s", job.preset_path, tail)
            return {
                "status": "error",
                "path": job.preset_path,
                "error": f"render process exited {proc.returncode}: {tail}",
            }
        result = parse_result_line(proc.stdout)
        if result is None:
            return {
                "status": "error",
                "path": job.preset_path,
                "error": "render process produced no result line",
            }
        if result.pop("audio_out", None) is not None:
            import numpy as np

            result["audio"] = np.load(audio_out)
        return result


def iter_jobs_isolated(
    jobs: list[Job],
    workers: int,
    serum1_plugin_path: str | None,
    serum2_plugin_path: str | None,
    sample_rate: int,
    keep_audio: bool = False,
) -> Iterator[dict]:
    """Deterministic-mode batch: every job renders in its own single-use
    process, fanned out across `workers` concurrent subprocesses.

    Bit-reproducible by construction — a fresh process is the isolation
    the cold-vs-cold ceiling measured as bit-identical, and the only
    reset that works for Serum 1 (in-process reload does not; see
    docs/decisions.md 2026-07-16). Threads only marshal subprocesses;
    DawDreamer runs in the children."""
    from concurrent.futures import ThreadPoolExecutor

    n_workers = resolve_worker_count(workers)
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [
            pool.submit(
                render_isolated,
                job,
                serum1_plugin_path,
                serum2_plugin_path,
                sample_rate,
                keep_audio,
            )
            for job in jobs
        ]
        for future in as_completed(futures):
            yield future.result()

"""loky executor management + batch dispatch. Main-process only —
workers import serum_render.engine, never this module."""
from __future__ import annotations

import logging
import os
from concurrent.futures import as_completed
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

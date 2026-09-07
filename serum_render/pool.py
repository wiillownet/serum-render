"""loky executor management + batch dispatch. Main-process only —
workers import serum_render.engine, never this module."""
from __future__ import annotations

import logging
import os
from concurrent.futures import FIRST_COMPLETED, wait
from pathlib import Path
from typing import Callable, Iterator

from loky import get_reusable_executor
from loky.process_executor import TerminatedWorkerError

from .engine import init_worker, run_job
from .jobs import Job

logger = logging.getLogger("serum_render")


class WorkerDied(RuntimeError):
    """A worker process was killed mid-batch (plugin crash, OOM kill). loky
    flags the executor broken and fails every pending future with the same
    message, so the batch stops here instead of reporting thousands of
    identical errors."""


def resolve_worker_count(workers: int) -> int:
    """-1 -> cpu_count - 1 (floor 1); otherwise max(1, workers)."""
    if workers == -1:
        return max(1, (os.cpu_count() or 2) - 1)
    return max(1, workers)


# Sample-based Serum 2 presets (Splice packs ship 90-160 MB files) cost
# ~3 GB of transient memory each to convert. They sort together by folder,
# so every worker hit one at once and 7 x 3 GB swapped a 16 GB machine.
_BIG_PRESET_BYTES = 16 * 2**20


def spread_big_presets(jobs: list[Job]) -> list[Job]:
    """Return `jobs` with presets over _BIG_PRESET_BYTES spaced evenly
    through the list, so the pool converts roughly one at a time.
    Everything else keeps its order. Files that cannot be stat'ed count
    as small; the worker reports them.

    ponytail: order heuristic, not a limit. A hard cap needs a
    cross-process semaphore loky cannot hand to workers.
    """
    big, small = [], []
    for job in jobs:
        try:
            is_big = os.path.getsize(job.preset_path) > _BIG_PRESET_BYTES
        except OSError:
            is_big = False
        (big if is_big else small).append(job)
    if not big or not small:
        return jobs
    out: list[Job] = []
    step = len(small) / len(big)
    for i, b in enumerate(big):
        out.extend(small[round(i * step):round((i + 1) * step)])
        out.append(b)
    return out


def _windowed(submit, jobs, n_workers: int, on_start):
    """Keep at most n_workers jobs submitted; yield (job, future) as each
    completes, refilling the window first so no worker idles while the
    caller handles a result.

    With the window equal to the worker count, a submit only happens when a
    worker is free, so "submitted" is "started" to within loky's hand-off.
    That is what makes `on_start` an honest per-job start callback without
    any worker-side reporting."""
    it = iter(jobs)
    pending: dict = {}

    def fill() -> None:
        while len(pending) < n_workers:
            job = next(it, None)
            if job is None:
                return
            if on_start is not None:
                on_start(job)
            pending[submit(job)] = job

    fill()
    while pending:
        done, _ = wait(pending, return_when=FIRST_COMPLETED)
        for future in done:
            job = pending.pop(future)
            fill()
            yield job, future


def iter_jobs(
    jobs: list[Job],
    workers: int,
    serum1_plugin_path: str | None,
    serum2_plugin_path: str | None,
    sample_rate: int,
    on_start: Callable[[Job], None] | None = None,
) -> Iterator[dict]:
    """
    Feed the reusable pool one job per free worker and yield result dicts
    as they complete (unordered — driven by whichever worker finishes
    first). `on_start(job)` fires in this process as each job is handed to
    a free worker.

    The 5-minute idle timeout keeps workers warm between batches; the
    executor is a process-wide singleton owned by loky. (loky has no
    per-job timeout, and workers only notice a dead parent when they pick
    up their next job, so this is also how long orphans linger.)

    With psutil installed (a dependency), loky recycles a worker whose
    memory grew more than 300 MB past its post-first-job baseline. A
    worker that converted one 160 MB sample-based preset holds ~2.7 GB
    for life otherwise, and seven of them exceed this machine's RAM.

    If a worker process crashes, loky permanently flags the executor
    broken and every remaining future raises: that surfaces once, as
    WorkerDied. Re-running with skip_existing=True is idempotent for the
    jobs that already landed on disk.
    """
    n_workers = resolve_worker_count(workers)
    executor = get_reusable_executor(
        max_workers=n_workers,
        initializer=init_worker,
        initargs=(serum1_plugin_path, serum2_plugin_path, sample_rate),
        timeout=300,
    )
    finished = 0
    for job, future in _windowed(
        lambda j: executor.submit(run_job, j), spread_big_presets(jobs), n_workers, on_start
    ):
        finished += 1
        try:
            yield future.result()
        except TerminatedWorkerError as exc:
            raise WorkerDied(
                "A worker process died (plugin crash or out of memory); "
                f"{len(jobs) - finished} preset(s) left unrendered. Re-run with "
                f"--skip-existing to resume. ({exc})"
            ) from exc
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
    # Mirrors the check in run_job, but before the subprocess spawns. The
    # warm pool amortizes plugin load across a worker's lifetime; here every
    # job pays it, so a skip would otherwise cost a full cold start.
    if (
        job.output_path is not None
        and job.skip_existing
        and Path(job.output_path).exists()
    ):
        return {"status": "skipped", "path": job.preset_path, "reason": "exists"}

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
    on_start: Callable[[Job], None] | None = None,
) -> Iterator[dict]:
    """Deterministic-mode batch: every job renders in its own single-use
    process, fanned out across `workers` concurrent subprocesses.
    `on_start(job)` fires as each subprocess is dispatched.

    Bit-reproducible by construction — a fresh process is the isolation
    the cold-vs-cold ceiling measured as bit-identical, and the only
    reset that works for Serum 1 (in-process reload does not; see
    docs/decisions.md 2026-07-16). Threads only marshal subprocesses;
    DawDreamer runs in the children."""
    from concurrent.futures import ThreadPoolExecutor

    n_workers = resolve_worker_count(workers)
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        submit = lambda job: pool.submit(  # noqa: E731
            render_isolated, job, serum1_plugin_path, serum2_plugin_path,
            sample_rate, keep_audio,
        )
        for _job, future in _windowed(submit, spread_big_presets(jobs), n_workers, on_start):
            yield future.result()

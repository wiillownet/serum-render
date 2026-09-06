"""iter_jobs error surfacing. The executor is stubbed: a real loky crash
test would need a plugin-free initializer the pool does not offer."""
from __future__ import annotations

from concurrent.futures import Future

import pytest
from loky.process_executor import TerminatedWorkerError

import serum_render.pool as pool
from serum_render.formats import PresetFormat
from serum_render.jobs import Job


class _StubExecutor:
    """Job 0 succeeds, job 1 raises like a normal per-job error, the rest
    fail the way loky fails every pending future after a worker dies."""

    def submit(self, fn, job):
        f: Future = Future()
        if job.note == 0:
            f.set_result({"status": "ok", "path": job.preset_path})
        elif job.note == 1:
            f.set_exception(ValueError("bad preset"))
        else:
            f.set_exception(TerminatedWorkerError("worker was killed"))
        return f


def test_iter_jobs_raises_worker_died_once(monkeypatch):
    monkeypatch.setattr(pool, "get_reusable_executor", lambda **kw: _StubExecutor())
    jobs = [Job(preset_path=f"/p{i}.fxp", format=PresetFormat.SERUM1, note=i) for i in range(4)]
    seen = []
    with pytest.raises(pool.WorkerDied, match="worker process died"):
        for r in pool.iter_jobs(jobs, 1, "/s1", None, 44100):
            seen.append(r["status"])
    # as_completed order among already-done futures is arbitrary, so only
    # the shape is checked: whatever arrived before the abort was a result.
    assert set(seen) <= {"ok", "error"}


def _jobs(tmp_path, sizes):
    jobs = []
    for i, size in enumerate(sizes):
        p = tmp_path / f"p{i:02d}.fxp"
        p.write_bytes(b"x" * size)
        jobs.append(Job(preset_path=str(p), format=PresetFormat.SERUM1))
    return jobs


def test_spread_big_presets_spaces_them_out(tmp_path):
    big = pool._BIG_PRESET_BYTES + 1
    jobs = _jobs(tmp_path, [1] * 6 + [big] * 3)
    out = pool.spread_big_presets(jobs)
    assert sorted(j.preset_path for j in out) == sorted(j.preset_path for j in jobs)
    big_positions = [i for i, j in enumerate(out) if j in jobs[6:]]
    assert big_positions == [2, 5, 8]
    # Small presets keep their relative order.
    smalls = [j for j in out if j in jobs[:6]]
    assert smalls == jobs[:6]


def test_spread_big_presets_noop_without_a_mix(tmp_path):
    small = _jobs(tmp_path, [1, 1])
    assert pool.spread_big_presets(small) == small
    missing = [Job(preset_path=str(tmp_path / "gone.fxp"), format=PresetFormat.SERUM1)]
    assert pool.spread_big_presets(missing) == missing

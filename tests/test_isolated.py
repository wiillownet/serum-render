"""Unit coverage for deterministic-mode plumbing that doesn't need a
plugin. The bit-reproducibility guarantee itself is exercised by the
smoke tests."""
from __future__ import annotations

import json
import subprocess
import sys

from serum_render.formats import PresetFormat
from serum_render.isolated import (
    RESULT_PREFIX,
    job_to_payload,
    parse_result_line,
)
from serum_render.jobs import Job


def test_isolated_module_import_is_stdlib_only():
    """isolated.py IS the render process — importing it must not load
    dawdreamer/numpy ahead of EngineHost's controlled import order."""
    code = (
        "import sys\n"
        "import serum_render.isolated\n"
        "for mod in ('numpy', 'dawdreamer', 'soundfile'):\n"
        "    assert mod not in sys.modules, f'{mod} leaked'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_payload_round_trips_job():
    job = Job(
        preset_path="/abs/p.SerumPreset",
        format=PresetFormat.SERUM2,
        note=60,
        output_path="/abs/out.wav",
    )
    payload = job_to_payload(job, None, "/plug/Serum2.vst3", 48000, None)
    # Must survive JSON (the wire format to the child process).
    decoded = json.loads(json.dumps(payload))
    rebuilt = Job(**decoded["job"])
    assert rebuilt == job
    assert PresetFormat(rebuilt.format) is PresetFormat.SERUM2
    assert decoded["sample_rate"] == 48000
    assert decoded["serum1_plugin_path"] is None


def test_parse_result_line_finds_result_amid_noise():
    stdout = (
        "Attempting to load VST: /Library/.../Serum.vst\n"
        "Initialising VST: Serum (1.3.6.8)\n"
        + RESULT_PREFIX
        + json.dumps({"status": "ok", "path": "/p.fxp"})
        + "\n"
    )
    assert parse_result_line(stdout) == {"status": "ok", "path": "/p.fxp"}


def test_parse_result_line_none_when_missing():
    assert parse_result_line("no result here\nat all\n") is None


def test_parse_result_line_takes_last_result():
    stdout = (
        RESULT_PREFIX + json.dumps({"status": "error"}) + "\n"
        + RESULT_PREFIX + json.dumps({"status": "ok"}) + "\n"
    )
    assert parse_result_line(stdout) == {"status": "ok"}


def test_render_isolated_skips_without_spawning(tmp_path, monkeypatch):
    """skip_existing must short-circuit in the parent: spawning the child
    pays a full plugin cold start just to learn the file is already there."""
    from serum_render import pool

    out = tmp_path / "done.wav"
    out.write_bytes(b"")
    job = Job(
        preset_path="/p.fxp",
        format=PresetFormat.SERUM1,
        output_path=str(out),
        skip_existing=True,
    )

    def boom(*args, **kwargs):
        raise AssertionError("subprocess spawned for an already-rendered file")

    monkeypatch.setattr(subprocess, "run", boom)
    result = pool.render_isolated(job, "/Serum.vst", None, 44100, False)
    assert result == {
        "status": "skipped", "path": "/p.fxp", "reason": "exists",
    }

"""Unit coverage for the render core that doesn't require a real plugin.
The full render path is exercised by the gated smoke tests."""
from __future__ import annotations

import subprocess
import sys
import threading

import pytest

import serum_render.engine as engine
from serum_render.engine import EngineHost, init_worker, run_job
from serum_render.formats import PresetFormat
from serum_render.jobs import Job


def _bare_host(engines: dict) -> EngineHost:
    """EngineHost with fakes injected, skipping __init__ (which would
    import dawdreamer and load real plugins)."""
    host = object.__new__(EngineHost)
    host.sample_rate = 44100
    host._engines = engines
    return host


class FakeSynth:
    def __init__(self, calls):
        self.calls = calls

    def load_preset(self, path):
        self.calls["load_preset_called_with"] = path

    def load_state(self, path):
        self.calls["load_state_called_with"] = path

    def clear_midi(self):
        self.calls["clear_midi_called"] = True

    def add_midi_note(self, *a, **kw):
        self.calls.setdefault("midi_notes", []).append(a)


class FakeEngine:
    def __init__(self, calls):
        self.calls = calls

    def render(self, duration):
        self.calls["render_called_with"] = duration

    def get_audio(self):
        import numpy as np

        return np.zeros((2, 4410), dtype="float32")


# ---- import hygiene ---------------------------------------------------


def test_engine_module_import_is_stdlib_only():
    """Importing serum_render.engine (as a fresh worker process does)
    must not transitively load dawdreamer or numpy — dawdreamer must be
    the first non-stdlib import in a render process, and that ordering
    happens inside EngineHost.__init__, not at module import."""
    code = (
        "import sys\n"
        "import serum_render.engine\n"
        "for mod in ('numpy', 'dawdreamer', 'soundfile', 'serum2_preset_loader'):\n"
        "    assert mod not in sys.modules, f'{mod} leaked into module-level imports'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# ---- constructor guards -------------------------------------------------


def test_enginehost_rejects_all_none_paths():
    """Fail before the dawdreamer import, not on first job dispatch."""
    with pytest.raises(ValueError, match="at least one"):
        EngineHost(None, None, 44100)


def test_init_worker_rejects_all_none_paths():
    with pytest.raises(ValueError, match="at least one"):
        init_worker(None, None, 44100)


# ---- render dispatch guards ---------------------------------------------


def test_render_rejects_unknown_preset_format():
    host = _bare_host({})
    job = Job(preset_path="/anywhere", format="vital")
    with pytest.raises(ValueError, match="Unknown preset format"):
        host.render(job)


def test_serum1_job_without_serum1_engine_raises():
    """A host built serum2-only that gets handed a serum1 job must fail
    clean, not None-deref."""
    calls: dict = {}
    host = _bare_host(
        {PresetFormat.SERUM2: engine._FormatEngine(FakeEngine(calls), FakeSynth(calls))}
    )
    job = Job(preset_path="/anywhere.fxp", format=PresetFormat.SERUM1)
    with pytest.raises(RuntimeError, match="no serum1 engine"):
        host.render(job)


def test_serum2_job_without_serum2_engine_raises():
    calls: dict = {}
    host = _bare_host(
        {PresetFormat.SERUM1: engine._FormatEngine(FakeEngine(calls), FakeSynth(calls))}
    )
    job = Job(preset_path="/anywhere.SerumPreset", format=PresetFormat.SERUM2)
    with pytest.raises(RuntimeError, match="no serum2 engine"):
        host.render(job)


def test_render_from_non_main_thread_raises():
    """DawDreamer hangs when driven from a thread — surface the foot-gun
    loudly before any engine call."""
    calls: dict = {}
    host = _bare_host(
        {PresetFormat.SERUM1: engine._FormatEngine(FakeEngine(calls), FakeSynth(calls))}
    )
    job = Job(preset_path="/anywhere.fxp", format=PresetFormat.SERUM1)
    caught: list[Exception] = []

    def target():
        try:
            host.render(job)
        except Exception as exc:
            caught.append(exc)

    t = threading.Thread(target=target)
    t.start()
    t.join()
    assert len(caught) == 1
    assert isinstance(caught[0], RuntimeError)
    assert "main thread" in str(caught[0])


# ---- serum2 dispatch ------------------------------------------------------


def test_serum2_dispatch_round_trips_state_blob(monkeypatch, tmp_path):
    """Serum 2 jobs must go convert_preset_file -> write_bytes ->
    load_state, never load_preset."""
    fake_blob = b"\x00\x01STATEBLOB\x02\x03"
    state_path = tmp_path / "state.bin"
    calls: dict = {}

    import serum2_preset_loader as spl

    def fake_convert(path):
        calls["convert_called_with"] = path
        return fake_blob

    monkeypatch.setattr(spl, "convert_preset_file", fake_convert)

    host = _bare_host(
        {
            PresetFormat.SERUM2: engine._FormatEngine(
                FakeEngine(calls), FakeSynth(calls), state_path=state_path
            )
        }
    )
    job = Job(
        preset_path="/some/preset.SerumPreset",
        format=PresetFormat.SERUM2,
        duration=0.1,
        tail=0.1,
    )
    host.render(job)

    assert calls.get("convert_called_with") == "/some/preset.SerumPreset"
    assert calls.get("load_state_called_with") == str(state_path)
    assert "load_preset_called_with" not in calls
    assert state_path.read_bytes() == fake_blob
    assert calls["render_called_with"] == pytest.approx(0.2)


def test_serum1_dispatch_calls_load_preset():
    calls: dict = {}
    host = _bare_host(
        {PresetFormat.SERUM1: engine._FormatEngine(FakeEngine(calls), FakeSynth(calls))}
    )
    job = Job(preset_path="/some/preset.fxp", format=PresetFormat.SERUM1)
    host.render(job)
    assert calls["load_preset_called_with"] == "/some/preset.fxp"
    assert calls["render_called_with"] == pytest.approx(2.0)  # duration + tail


def test_midi_render_duration_uses_precomputed_length():
    calls: dict = {}
    host = _bare_host(
        {PresetFormat.SERUM1: engine._FormatEngine(FakeEngine(calls), FakeSynth(calls))}
    )

    class MidiFakeSynth(FakeSynth):
        def load_midi(self, path, **kw):
            self.calls["load_midi_called_with"] = (path, kw)

    host._engines[PresetFormat.SERUM1].synth = MidiFakeSynth(calls)
    job = Job(
        preset_path="/some/preset.fxp",
        format=PresetFormat.SERUM1,
        midi_path="/abs/seq.mid",
        midi_duration=3.0,
        tail=0.5,
    )
    host.render(job)
    path, kw = calls["load_midi_called_with"]
    assert path == "/abs/seq.mid"
    assert kw == {"clear_previous": True, "beats": False, "all_events": True}
    assert calls["render_called_with"] == pytest.approx(3.5)


# ---- run_job --------------------------------------------------------------


def test_run_job_before_init_worker_returns_error(monkeypatch):
    monkeypatch.setattr(engine, "_HOST", None)
    job = Job(preset_path="/p.fxp", format=PresetFormat.SERUM1)
    result = run_job(job)
    assert result["status"] == "error"
    assert "init_worker" in result["error"]


def test_run_job_skip_existing(monkeypatch, tmp_path):
    out = tmp_path / "done.wav"
    out.write_bytes(b"already here")
    calls: dict = {}
    monkeypatch.setattr(
        engine,
        "_HOST",
        _bare_host(
            {PresetFormat.SERUM1: engine._FormatEngine(FakeEngine(calls), FakeSynth(calls))}
        ),
    )
    job = Job(
        preset_path="/p.fxp",
        format=PresetFormat.SERUM1,
        output_path=str(out),
        skip_existing=True,
    )
    result = run_job(job)
    assert result == {"status": "skipped", "path": "/p.fxp"}
    assert "render_called_with" not in calls  # never touched the engine


def test_run_job_writes_to_disk_and_returns_status_only(monkeypatch, tmp_path):
    calls: dict = {}
    monkeypatch.setattr(
        engine,
        "_HOST",
        _bare_host(
            {PresetFormat.SERUM1: engine._FormatEngine(FakeEngine(calls), FakeSynth(calls))}
        ),
    )
    out = tmp_path / "a.wav"
    job = Job(
        preset_path="/p.fxp",
        format=PresetFormat.SERUM1,
        output_path=str(out),
    )
    result = run_job(job)
    assert result == {"status": "ok", "path": "/p.fxp"}
    assert out.exists()


def test_run_job_returns_audio_when_no_output_path(monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(
        engine,
        "_HOST",
        _bare_host(
            {PresetFormat.SERUM1: engine._FormatEngine(FakeEngine(calls), FakeSynth(calls))}
        ),
    )
    job = Job(preset_path="/p.fxp", format=PresetFormat.SERUM1)
    result = run_job(job)
    assert result["status"] == "ok"
    assert result["audio"].shape == (2, 4410)


def test_run_job_wraps_render_errors(monkeypatch):
    class ExplodingEngine(FakeEngine):
        def render(self, duration):
            raise RuntimeError("plugin exploded")

    calls: dict = {}
    monkeypatch.setattr(
        engine,
        "_HOST",
        _bare_host(
            {
                PresetFormat.SERUM1: engine._FormatEngine(
                    ExplodingEngine(calls), FakeSynth(calls)
                )
            }
        ),
    )
    job = Job(preset_path="/p.fxp", format=PresetFormat.SERUM1)
    result = run_job(job)
    assert result["status"] == "error"
    assert "plugin exploded" in result["error"]

"""Unit coverage for the render core that doesn't require a real plugin.
The full render path is exercised by the gated smoke tests."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
from pathlib import Path

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
        # The file is gone after the render, so keep what the load saw.
        self.calls["state_bytes"] = Path(path).read_bytes()

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


@pytest.mark.parametrize("found", ["0.1.3", "0.3.0", "0.3.9.post1", "0.4.0", "0.5.2"])
def test_enginehost_rejects_old_serum2_loader(monkeypatch, found):
    """Loaders before 0.6 keep MPE on in the state and pass bad FX entries
    to Serum. Fail before the dawdreamer import."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: found)
    with pytest.raises(engine.LoaderVersionError, match=f"{found} is too old"):
        EngineHost(None, "/anywhere/Serum2.vst3", 44100)


@pytest.mark.parametrize("found", ["0.6.0", "0.6.1", "0.10.0", "1.0.0"])
def test_check_serum2_loader_accepts_0_6_and_later(monkeypatch, found):
    monkeypatch.setattr("importlib.metadata.version", lambda name: found)
    engine.check_serum2_loader()


def test_loader_version_messages_name_the_pin(monkeypatch):
    """Both messages give the install command the pin in pyproject allows."""
    from importlib.metadata import PackageNotFoundError

    pin = "serum2-preset-loader>=0.6,<0.7"
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.5.2")
    with pytest.raises(engine.LoaderVersionError, match="0.5.2 is too old") as old:
        engine.check_serum2_loader()
    assert pin in str(old.value)

    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", missing)
    with pytest.raises(engine.LoaderVersionError, match="not installed") as absent:
        engine.check_serum2_loader()
    assert pin in str(absent.value)


def test_pyproject_pin_matches_the_version_check():
    """The floor in the code and the one in pyproject move together."""
    import tomllib
    from pathlib import Path

    root = Path(engine.__file__).resolve().parent.parent
    deps = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    major, minor = engine.MIN_SERUM2_LOADER
    assert f"serum2-preset-loader>={major}.{minor},<{major}.{minor + 1}" in deps


# ---- real loader through the render core --------------------------------


def _preset_tree(**extra) -> dict:
    return {
        "presetName": "Tiny", "presetAuthor": "Tester", "presetDescription": "",
        "product": "Serum2", "url": "https://xferrecords.com/", "vendor": "Xfer Records",
        "productVersion": "2.1.5", "version": 11.0, "fileType": "SerumPreset",
        **extra,
    }


def _write_preset(path, tree) -> str:
    from serum2_preset_loader import encode_preset

    header = {k: tree[k] for k in ("presetName", "presetAuthor", "presetDescription",
                                   "product", "productVersion", "fileType")}
    path.write_bytes(encode_preset(header, tree))
    return str(path)


def _state_tree(state: bytes) -> dict:
    """The processor tree inside a JUCE state blob."""
    import struct
    import xml.etree.ElementTree as ET

    import cbor2
    from serum2_preset_loader import juce_memoryblock_b64decode, unwrap_xferjson

    xml_len = struct.unpack("<I", state[4:8])[0]
    root = ET.fromstring(state[8:8 + xml_len].decode("utf-8"))
    _meta, _version, cbor = unwrap_xferjson(juce_memoryblock_b64decode(root.findtext("IComponent")))
    return cbor2.loads(cbor)


def test_serum2_job_leaves_mpe_out_of_the_state(tmp_path):
    """A file with MPE on reaches Serum with MPE off, as an editor load does."""
    calls: dict = {}
    fe = engine._FormatEngine(FakeEngine(calls), FakeSynth(calls))
    host = _bare_host({PresetFormat.SERUM2: fe})
    preset = _write_preset(
        tmp_path / "mpe.SerumPreset",
        _preset_tree(mpeEnabled=True, mpeConfig=1, mpePitchBendRange=24),
    )
    host.render(Job(preset, PresetFormat.SERUM2))
    state = _state_tree(calls["state_bytes"])
    assert state["component"] == "processor"
    assert not {"mpeEnabled", "mpeConfig", "mpePitchBendRange"} & set(state)


def test_bad_fx_entry_fails_the_job_not_the_worker(tmp_path, monkeypatch):
    """InvalidFXTypeError becomes an error result, before Serum sees the state."""
    calls: dict = {}
    fe = engine._FormatEngine(FakeEngine(calls), FakeSynth(calls))
    monkeypatch.setattr(engine, "_HOST", _bare_host({PresetFormat.SERUM2: fe}))
    preset = _write_preset(
        tmp_path / "bad.SerumPreset",
        _preset_tree(FXRack0={"FX": [{"type": 99}]}),
    )
    result = run_job(Job(preset, PresetFormat.SERUM2))
    assert result["status"] == "error" and "FX[0]" in result["error"]
    assert "load_state_called_with" not in calls


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
    calls: dict = {}

    import serum2_preset_loader as spl

    def fake_convert(path, keep_mpe=False):
        calls["convert_called_with"] = path
        return fake_blob

    monkeypatch.setattr(spl, "convert_preset_file", fake_convert)
    monkeypatch.setattr(spl, "decode_preset_file", lambda path: ({}, {}))
    monkeypatch.setattr(spl, "read_preset_metadata", lambda path: {"productVersion": "2.1.3"})

    def fake_versions(state, header):
        calls["versions_called_with"] = (state, header)
        return {"preset_version": "2.1.3"}

    monkeypatch.setattr(engine, "preset_versions", fake_versions)

    host = _bare_host(
        {
            PresetFormat.SERUM2: engine._FormatEngine(
                FakeEngine(calls), FakeSynth(calls)
            )
        }
    )
    job = Job(
        preset_path="/some/preset.SerumPreset",
        format=PresetFormat.SERUM2,
        duration=0.1,
        tail=0.1,
    )
    _audio, versions = host.render(job)

    assert versions == {"preset_version": "2.1.3"}
    assert calls["versions_called_with"] == (fake_blob, {"productVersion": "2.1.3"})
    assert calls.get("convert_called_with") == "/some/preset.SerumPreset"
    assert Path(calls["load_state_called_with"]).name == "state.bin"
    assert "load_preset_called_with" not in calls
    assert calls["state_bytes"] == fake_blob
    assert calls["render_called_with"] == pytest.approx(0.2)


def test_unreadable_preset_version_does_not_fail_the_render(monkeypatch, tmp_path):
    import serum2_preset_loader as spl

    calls: dict = {}
    monkeypatch.setattr(spl, "convert_preset_file", lambda path, keep_mpe=False: b"\x00STATE")
    monkeypatch.setattr(spl, "decode_preset_file", lambda path: ({}, {}))
    monkeypatch.setattr(spl, "read_preset_metadata", lambda path: {})
    host = _bare_host(
        {
            PresetFormat.SERUM2: engine._FormatEngine(
                FakeEngine(calls), FakeSynth(calls)
            )
        }
    )
    job = Job(preset_path="/p.SerumPreset", format=PresetFormat.SERUM2, duration=0.1, tail=0.1)
    _audio, versions = host.render(job)

    assert versions["preset_version"] is None
    assert "state layout changed" in versions["preset_version_error"]
    assert Path(calls["load_state_called_with"]).name == "state.bin"


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
    assert result == {
        "status": "skipped", "path": "/p.fxp", "reason": "exists",
    }
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
    assert result["status"] == "ok"
    assert result["path"] == "/p.fxp"
    # The point of the disk path: no audio array crosses the process
    # boundary. `peak` is the only extra, and it is a scalar.
    assert "audio" not in result
    assert result["peak"] == 0.0
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


def test_run_job_exits_when_parent_is_gone(monkeypatch):
    exited: list[int] = []

    def fake_exit(code):
        exited.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(engine, "_PARENT_PID", -1)  # never our real parent
    monkeypatch.setattr(engine.os, "_exit", fake_exit)
    with pytest.raises(SystemExit):
        run_job(Job(preset_path="/p.fxp", format=PresetFormat.SERUM1))
    assert exited == [0]


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


def test_run_job_error_text_is_never_blank(monkeypatch):
    """str(MemoryError()) is empty; the FAIL line and --json event used to
    carry a blank error."""
    class OOMEngine(FakeEngine):
        def render(self, duration):
            raise MemoryError()

    calls: dict = {}
    monkeypatch.setattr(
        engine,
        "_HOST",
        _bare_host(
            {PresetFormat.SERUM1: engine._FormatEngine(OOMEngine(calls), FakeSynth(calls))}
        ),
    )
    result = run_job(Job(preset_path="/p.fxp", format=PresetFormat.SERUM1))
    assert result["status"] == "error"
    assert result["error"] == "MemoryError"


def test_corrupt_serum2_preset_is_a_per_preset_error(monkeypatch, tmp_path):
    """A well-formed envelope around a corrupt payload goes through the real
    serum2-preset-loader and comes back as one failed preset with a readable
    message. Before 0.1.4 this read "'int' object has no attribute 'pop'"."""
    import zstandard
    from serum2_preset_loader import wrap_xferjson_precompressed

    calls: dict = {}
    payload = b"\x05"  # CBOR for the integer 5, not a map
    bad = tmp_path / "corrupt.SerumPreset"
    bad.write_bytes(
        wrap_xferjson_precompressed(
            {"fileType": "SerumPreset"},
            zstandard.ZstdCompressor().compress(payload),
            uncompressed_size=len(payload),
        )
    )
    monkeypatch.setattr(
        engine,
        "_HOST",
        _bare_host(
            {
                PresetFormat.SERUM2: engine._FormatEngine(
                    FakeEngine(calls), FakeSynth(calls)
                )
            }
        ),
    )
    result = run_job(Job(preset_path=str(bad), format=PresetFormat.SERUM2))
    assert result["status"] == "error"
    assert "CBOR payload must be a map" in result["error"]


def test_keep_mpe_keeps_the_files_mpe_settings(tmp_path):
    """keep_mpe=True puts the file's MPE keys in the state, unchanged."""
    calls: dict = {}
    fe = engine._FormatEngine(FakeEngine(calls), FakeSynth(calls))
    host = _bare_host({PresetFormat.SERUM2: fe})
    preset = _write_preset(
        tmp_path / "mpe.SerumPreset",
        _preset_tree(mpeEnabled=True, mpeConfig=1, mpePitchBendRange=24),
    )
    _, versions = host.render(Job(preset, PresetFormat.SERUM2, keep_mpe=True))
    state = _state_tree(calls["state_bytes"])
    assert (state["mpeEnabled"], state["mpeConfig"], state["mpePitchBendRange"]) == (True, 1, 24)
    assert versions["keep_mpe"] is True

    _, versions = host.render(Job(preset, PresetFormat.SERUM2))
    assert "keep_mpe" not in versions


# ---- temp folder lifetime ---------------------------------------------


def _stub_loader(monkeypatch):
    import serum2_preset_loader as spl

    monkeypatch.setattr(spl, "convert_preset_file", lambda path, keep_mpe=False: b"\x00STATE")
    monkeypatch.setattr(spl, "decode_preset_file", lambda path: ({}, {}))
    monkeypatch.setattr(spl, "read_preset_metadata", lambda path: {})


def _leftovers(root):
    return list(root.glob("serum_render_*"))


def test_batch_leaves_no_temp_folder(monkeypatch, tmp_path):
    _stub_loader(monkeypatch)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    calls: dict = {}
    host = _bare_host({PresetFormat.SERUM2: engine._FormatEngine(FakeEngine(calls), FakeSynth(calls))})
    seen = []
    for i in range(3):
        host.render(Job(f"/p{i}.SerumPreset", PresetFormat.SERUM2))
        seen.append(calls["load_state_called_with"])
        assert _leftovers(tmp_path) == []
    # The load saw a real file in a folder of ours, a new one each render.
    assert all(Path(p).parent.parent == tmp_path for p in seen) and len(set(seen)) == 3


def test_failed_load_leaves_no_temp_folder(monkeypatch, tmp_path):
    _stub_loader(monkeypatch)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    class BadSynth(FakeSynth):
        def load_state(self, path):
            assert Path(path).exists()
            raise RuntimeError("plugin refused the state")

    calls: dict = {}
    host = _bare_host({PresetFormat.SERUM2: engine._FormatEngine(FakeEngine(calls), BadSynth(calls))})
    with pytest.raises(RuntimeError, match="plugin refused"):
        host.render(Job("/p.SerumPreset", PresetFormat.SERUM2))
    assert _leftovers(tmp_path) == []


def test_failed_render_after_load_leaves_no_temp_folder(monkeypatch, tmp_path):
    _stub_loader(monkeypatch)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    class BadEngine(FakeEngine):
        def render(self, duration):
            raise RuntimeError("render failed")

    calls: dict = {}
    host = _bare_host({PresetFormat.SERUM2: engine._FormatEngine(BadEngine(calls), FakeSynth(calls))})
    with pytest.raises(RuntimeError, match="render failed"):
        host.render(Job("/p.SerumPreset", PresetFormat.SERUM2))
    assert _leftovers(tmp_path) == []


def test_constructing_a_host_makes_no_temp_folder():
    """A process that exits between jobs must have nothing to clean up: the
    folder belongs to the render, not the host."""
    import inspect

    assert "mkdtemp" not in inspect.getsource(EngineHost)

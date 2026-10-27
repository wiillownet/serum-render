"""Provenance fields: the state header parse, header/body comparison and
plugin version lookup. No plugin or real preset needed."""
from __future__ import annotations

import json
import plistlib
import struct
from importlib.metadata import version

import pytest
from serum2_preset_loader import build_juce_vst3_state

from serum_render import provenance
from serum_render.formats import PresetFormat


def _state(meta: dict, payload: bytes) -> bytes:
    """Same envelope the converter emits: XferJson header, then payload,
    inside a JUCE VST3 state."""
    js = json.dumps(meta).encode()
    icomponent = b"XferJson\x00" + struct.pack("<Q", len(js)) + js + payload
    return build_juce_vst3_state(icomponent)


# Header lengths on every residue mod 3, so the prefix rounding is exercised.
@pytest.mark.parametrize("pad", ["", "x", "xx"])
# ids: the default id embeds the bytes, and Windows rejects the 100 KB one in
# PYTEST_CURRENT_TEST (env vars cap at 32767 chars).
@pytest.mark.parametrize("payload", [b"", b"\x00" * 8, bytes(range(256)) * 400],
                         ids=lambda b: f"{len(b)}B")
def test_state_metadata_reads_header_from_prefix(pad, payload):
    meta = {"component": "processor", "productVersion": "2.1.3", "pad": pad}
    assert provenance._state_metadata(_state(meta, payload)) == meta


def test_state_metadata_rejects_non_xferjson():
    with pytest.raises(ValueError, match="not XferJson"):
        provenance._state_metadata(build_juce_vst3_state(b"NotXfer!!" + b"\x00" * 40))


def test_state_metadata_names_a_missing_icomponent():
    with pytest.raises(ValueError, match="state layout changed"):
        provenance._state_metadata(b"\x00\x01STATEBLOB\x02\x03")


def test_preset_versions_agree():
    state = _state({"productVersion": "2.1.3"}, b"\x00" * 8)
    assert provenance.preset_versions(state, {"productVersion": "2.1.3"}) == {
        "preset_version": "2.1.3"
    }


def test_preset_versions_disagree_keeps_both_and_flags():
    state = _state({"productVersion": "2.1.3"}, b"\x00" * 8)
    assert provenance.preset_versions(state, {"productVersion": "2.1.4"}) == {
        "preset_version": "2.1.3",
        "preset_header_version": "2.1.4",
        "preset_version_mismatch": True,
    }


def test_plugin_version_reads_macos_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance.sys, "platform", "darwin")
    bundle = tmp_path / "Serum2.vst3"
    (bundle / "Contents").mkdir(parents=True)
    with (bundle / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleShortVersionString": "2.1.5"}, f)
    assert provenance.plugin_version(bundle) == "2.1.5"
    assert provenance.plugin_version(tmp_path / "missing.vst3") is None


def test_plugin_version_is_none_off_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance.sys, "platform", "win32")
    assert provenance.plugin_version(tmp_path) is None


def test_run_provenance_covers_only_formats_in_use(tmp_path):
    prov = provenance.run_provenance({PresetFormat.SERUM2: tmp_path / "x.vst3"})
    assert set(prov["plugins"]) == {"serum2"}
    assert prov["serum2_preset_loader"] == version("serum2-preset-loader")

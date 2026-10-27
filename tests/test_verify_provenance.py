"""scripts/verify_provenance.py against hand-written logs. No plugin needed:
rows with an empty `plugins` dict skip the plugin check."""
from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_provenance.py"


def _row(output: str, preset: str, **over) -> dict:
    row = {
        "output": output, "preset": preset, "preset_version": "2.0.11",
        "serum_render": version("serum-render"),
        "serum2_preset_loader": version("serum2-preset-loader"),
        "plugins": {},
    }
    row.update(over)
    return row


def _run(out: Path, rows: list[dict], listing: list[str] | None = None):
    (out / "serum-render-provenance.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    args = [sys.executable, str(SCRIPT), str(out)]
    if listing is not None:
        (out.parent / "list.txt").write_text("\n".join(listing) + "\n", encoding="utf-8")
        args.append(str(out.parent / "list.txt"))
    return subprocess.run(args, capture_output=True, text=True)


def test_clean_folder_passes(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.wav").write_bytes(b"")
    res = _run(out, [_row("a.wav", "/p/a.SerumPreset")], ["/p/a.SerumPreset"])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "1 files logged | wrong versions 0" in res.stdout


def test_last_line_per_output_wins(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.wav").write_bytes(b"")
    old = _row("a.wav", "/p/a.SerumPreset", serum2_preset_loader="0.1.3")
    res = _run(out, [old, _row("a.wav", "/p/a.SerumPreset")])
    assert res.returncode == 0, res.stdout + res.stderr


def test_each_failure_kind_fails(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for name in "abc":
        (out / f"{name}.wav").write_bytes(b"")
    rows = [
        _row("a.wav", "/p/a.SerumPreset", serum2_preset_loader="0.1.3"),
        _row("b.wav", "/p/b.SerumPreset", preset_version_mismatch=True),
        _row("c.wav", "/p/c.SerumPreset", plugins={"serum2": "0.0.0-not-installed"}),
        _row("gone.wav", "/p/gone.SerumPreset"),
    ]
    res = _run(out, rows, ["/p/a.SerumPreset", "/p/missing.SerumPreset"])
    assert res.returncode == 1
    assert ("4 files logged | wrong versions 2 | version mismatch/error 1 | "
            "listed presets without a line 1 | logged but no file 1") in res.stdout


def test_missing_log_is_an_error(tmp_path):
    res = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path)],
                         capture_output=True, text=True)
    assert res.returncode == 1
    assert "no serum-render-provenance.jsonl" in res.stderr

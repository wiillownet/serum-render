"""
Check a render folder's serum-render-provenance.jsonl against this install.

For every file the log describes (the last line per `output` wins), checks
that serum-render, serum2-preset-loader and the stock-path plugins match the
versions installed now, that no preset flagged a version mismatch or read
error, and that the file is on disk. Given a preset list (one path per line,
e.g. scripts/rerender_list.py's stdout), also checks every listed preset has
a line.

Run from the repo root after a render:
    .venv/bin/python scripts/verify_provenance.py out/ [rerender.txt]

Exits 1 when anything fails, 0 when clean.
"""
import json
import sys
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from serum_render.config import default_plugin_path
from serum_render.formats import PresetFormat
from serum_render.provenance import PROVENANCE_FILE, plugin_version

if not 2 <= len(sys.argv) <= 3 or not Path(sys.argv[1]).is_dir():
    sys.exit(f"usage: {sys.argv[0]} <output dir> [preset list]")
out = Path(sys.argv[1])
log = out / PROVENANCE_FILE
if not log.is_file():
    sys.exit(f"no {PROVENANCE_FILE} in {out}: nothing rendered there with serum-render 0.6+")

expected = {
    "serum_render": version("serum-render"),
    "serum2_preset_loader": version("serum2-preset-loader"),
}
# shortcut: plugins are checked at their stock install paths only; a render
# made with --serum1/--serum2 elsewhere reports a plugin version mismatch.
plugins = {}
for fmt in PresetFormat:
    path = default_plugin_path(fmt)
    plugins[fmt.value] = plugin_version(path) if path else None

last = {}
with log.open(encoding="utf-8") as f:
    for n, line in enumerate(f, 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.exit(f"{log}:{n}: not JSON ({exc})")
        last[row["output"]] = row
rows = list(last.values())

wrong = [
    r for r in rows
    if any(r.get(k) != v for k, v in expected.items())
    or any(plugins.get(fmt) != v for fmt, v in r.get("plugins", {}).items())
]
flagged = [r for r in rows if r.get("preset_version_mismatch") or r.get("preset_version_error")]
no_file = [r["output"] for r in rows if not (out / r["output"]).exists()]
no_line = []
if len(sys.argv) == 3:
    logged = {r["preset"] for r in rows}
    no_line = [p for p in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
               if p.strip() and p.strip() not in logged]

print(f"{len(rows)} files logged | wrong versions {len(wrong)} | version mismatch/error "
      f"{len(flagged)} | listed presets without a line {len(no_line)} | logged but no file {len(no_file)}")
print("expected:", expected, "plugins:", plugins)
print("preset_version:", Counter(r.get("preset_version") for r in rows).most_common(8))
for r in wrong[:10]:
    print("  wrong versions:", r["output"], {k: r.get(k) for k in expected}, r.get("plugins"))
for r in flagged[:10]:
    print("  flagged:", r["output"])
for p in no_line[:10]:
    print("  no line:", p)
for o in no_file[:10]:
    print("  no file:", o)
sys.exit(1 if wrong or flagged or no_line or no_file else 0)

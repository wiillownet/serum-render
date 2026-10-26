"""
List .SerumPreset files that serum2-preset-loader < 0.4 rendered wrong.

Loaders before 0.4 stamped every state 2.1.4 / 10.0, so Serum skipped its
migration for older presets; 0.4+ keeps the preset's own (productVersion,
version). Affected: a tree productVersion below 2.1.4, or none at all.
Renders of those presets made with an older loader need redoing.

Run from the repo root:
    .venv/bin/python scripts/rerender_list.py \
        "/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets" > rerender.txt

Affected preset paths go to stdout; the summary count, unaffected presets
stamped anything other than 2.1.4 / 10.0, and unreadable files go to stderr.
"""
import re
import sys
from pathlib import Path

from serum2_preset_loader import decode_preset_file


def vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


roots = [Path(r) for r in sys.argv[1:]]
if not roots or not all(r.is_dir() for r in roots):
    sys.exit(f"usage: {sys.argv[0]} <preset dir>...  (each must be an existing directory)")

affected = other = 0
for root in roots:
    for p in sorted(root.rglob("*.SerumPreset")):
        try:
            _meta, tree = decode_preset_file(p)
        except (OSError, ValueError) as exc:
            print(f"UNREADABLE {p}: {exc}", file=sys.stderr)
            continue
        pv = tree.get("productVersion")
        # No usable version: old loaders stamped 2.1.4 on it, 0.4+ stamps
        # 2.1.5, so its render changed too.
        if not isinstance(pv, str) or vtuple(pv) < (2, 1, 4):
            print(p)
            affected += 1
        else:
            if (pv, tree.get("version")) != ("2.1.4", 10.0):
                print(f"OTHER-STAMP {pv} {tree.get('version')} {p}", file=sys.stderr)
            other += 1
print(f"{affected} affected (< 2.1.4), {other} not", file=sys.stderr)

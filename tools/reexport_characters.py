"""Re-export character .blend -> assets/models/*.gltf with raylib-safe skinning.

Convenience wrapper around tools/export_characters.py: it locates Blender and the
"Ultimate Animated Character Pack" Blends folder, then drives Blender headless to
re-export. This is the fix for "squiggly arms and legs" — see export_characters.py
for the why (raylib caps bone influences at 4 per vertex).

    # re-export everything in the pack -> assets/models/
    .venv/bin/python tools/reexport_characters.py

    # re-export just a few (matched by .blend stem, case-insensitive)
    .venv/bin/python tools/reexport_characters.py Casual_Male Suit_Female

Override discovery with env vars if your paths differ:
    BLENDER=/Applications/Blender.app/Contents/MacOS/Blender
    CHAR_PACK="/Users/owner/Documents/Ultimate Animated Character Pack - Nov 2019/Blends"
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "models")
EXPORT_SCRIPT = os.path.join(ROOT, "tools", "export_characters.py")

_BLENDER_CANDIDATES = [
    os.environ.get("BLENDER"),
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "blender",
]
_PACK_CANDIDATES = [
    os.environ.get("CHAR_PACK"),
    os.path.expanduser("~/Documents/Ultimate Animated Character Pack - Nov 2019/Blends"),
]


def _first_existing(paths, kind: str, allow_path_lookup=False) -> str:
    for p in paths:
        if not p:
            continue
        if os.path.exists(p):
            return p
        if allow_path_lookup and not os.sep in p:  # bare command name on PATH
            from shutil import which
            found = which(p)
            if found:
                return found
    sys.exit(f"Could not find {kind}. Set it via env var. Tried: "
             + ", ".join(str(p) for p in paths if p))


def main(argv: list[str]) -> int:
    blender = _first_existing(_BLENDER_CANDIDATES, "Blender", allow_path_lookup=True)
    pack = _first_existing(_PACK_CANDIDATES, "the character pack Blends folder")

    wanted = {a.lower().removesuffix(".blend") for a in argv}
    all_blends = sorted(glob.glob(os.path.join(pack, "*.blend")))
    if wanted:
        all_blends = [b for b in all_blends
                      if os.path.splitext(os.path.basename(b))[0].lower() in wanted]
        if not all_blends:
            sys.exit(f"None of {sorted(wanted)} found in {pack}")

    print(f"Blender: {blender}")
    print(f"Source : {pack}")
    print(f"Output : {OUT}")
    os.makedirs(OUT, exist_ok=True)

    failures = []
    for blend in all_blends:
        stem = os.path.splitext(os.path.basename(blend))[0]
        cmd = [blender, "--background", blend, "--python", EXPORT_SCRIPT, "--", OUT]
        print(f"\n=== {stem} ===")
        # Blender is chatty + returns 0 even on python errors, so scan output.
        proc = subprocess.run(cmd, capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        ok = os.path.exists(os.path.join(OUT, stem + ".gltf")) and "Error" not in out
        for line in out.splitlines():
            if line.startswith(("  ->", "  (note", "Error", "Traceback")) or "Exception" in line:
                print(line)
        if not ok:
            failures.append(stem)
            print(out[-1500:])

    print(f"\nExported {len(all_blends) - len(failures)}/{len(all_blends)} characters.")
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

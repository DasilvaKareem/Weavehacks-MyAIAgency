"""Re-export Quaternius character .blend files to raylib-safe .gltf.

WHY THIS EXISTS — "squiggly arms and legs"
------------------------------------------
raylib does GPU vertex skinning with EXACTLY 4 bone influences per vertex
(see game/skinning.py: vertexBoneIds / vertexBoneWeights are vec4) and up to
128 bones. If Blender exports a skinned mesh with MORE than 4 influences per
vertex (JOINTS_1/WEIGHTS_1 sets), or with un-normalized weights, raylib silently
reads only the first 4 and the limbs explode into spikes — even with the GPU
skinning shader. The fix is to cap influences at 4 on export.

This reproduces the exact shape of the working files in assets/models/ (made by
"Khronos glTF Blender I/O"): embedded .gltf, +Y up, skin + all actions as clips,
4-influence cap, no morph targets.

This script runs INSIDE Blender (it needs `bpy`). Run it via the Blender CLI:

    # one file -> assets/models/Casual_Male.gltf
    /Applications/Blender.app/Contents/MacOS/Blender --background \
        "/path/to/Casual_Male.blend" --python tools/export_characters.py -- out_dir

    # whole folder (every *.blend -> <out_dir>/<Name>.gltf), no --background file:
    /Applications/Blender.app/Contents/MacOS/Blender --background \
        --python tools/export_characters.py -- --src "/path/to/Blends" --out assets/models

Args after the `--` separator:
    --src  DIR     export every *.blend in DIR (batch mode)
    --out  DIR     output directory for .gltf (default: assets/models)
    [POSITIONAL]   if a single .blend was opened via the CLI, a lone positional
                   arg is treated as the output dir.

Prefer the convenience wrapper which finds Blender + the source pack for you:
    .venv/bin/python tools/reexport_characters.py [Name ...]
"""
from __future__ import annotations

import base64
import json
import os
import sys

try:
    import bpy  # type: ignore
except ImportError:  # pragma: no cover - only meaningful inside Blender
    sys.exit("export_characters.py must be run inside Blender (no `bpy` module). "
             "Use tools/reexport_characters.py, or pass it to Blender with --python.")


def _argv_after_ddash() -> list[str]:
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def _parse(args: list[str]) -> tuple[str | None, str]:
    """Return (src_dir_or_None, out_dir). src=None means 'export the open file'."""
    src, out, pos = None, None, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--src":
            src = args[i + 1]; i += 2
        elif a == "--out":
            out = args[i + 1]; i += 2
        else:
            pos.append(a); i += 1
    if out is None:
        out = pos[0] if pos else os.path.join(os.getcwd(), "assets", "models")
    return src, os.path.abspath(out)


# raylib-safe export options. We pass only the keys the installed Blender's glTF
# operator actually accepts (names drift across versions), so this stays robust.
_DESIRED = {
    # Blender 5.1 dropped GLTF_EMBEDDED; we export SEPARATE (.gltf + .bin) and
    # then inline the .bin/textures back to base64 ourselves (see _embed_gltf),
    # reproducing the self-contained text .gltf the loader expects.
    "export_format": "GLTF_SEPARATE",
    "use_selection": False,
    "use_visible": False,
    "use_active_collection": False,
    "export_yup": True,                  # glTF/raylib are +Y up
    "export_apply": False,               # never apply modifiers on a rigged mesh
    "export_texcoords": True,
    "export_normals": True,
    "export_tangents": False,
    "export_materials": "EXPORT",
    "export_skins": True,
    "export_all_influences": False,      # <-- THE squiggly fix: cap 4 weights/vertex
    "export_morph": False,
    "export_animations": True,
    "export_animation_mode": "ACTIONS",  # each action -> its own named clip
    "export_nla_strips": True,
    "export_anim_single_armature": True,
    "export_bake_animation": True,
    "export_optimize_animation_size": False,
    "export_def_bones": False,
}


def _supported_kwargs() -> dict:
    rna = bpy.ops.export_scene.gltf.get_rna_type()
    valid = set(rna.properties.keys())
    kept = {k: v for k, v in _DESIRED.items() if k in valid}
    dropped = [k for k in _DESIRED if k not in valid]
    if dropped:
        print(f"  (note: this Blender ignores {dropped} — using its defaults)")
    return kept


_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".bin": "application/octet-stream"}


def _embed_gltf(gltf_path: str) -> None:
    """Inline external .bin + image files into the .gltf as base64 data URIs, then
    delete the now-orphaned sidecars. Produces the self-contained text .gltf that
    Blender's old GLTF_EMBEDDED made (and that assets.py reads for material tints)."""
    base = os.path.dirname(gltf_path)
    with open(gltf_path) as fh:
        doc = json.load(fh)
    sidecars: set[str] = set()

    def inline(uri: str) -> str:
        if not uri or uri.startswith("data:"):
            return uri
        path = os.path.join(base, uri)
        if not os.path.exists(path):
            return uri
        sidecars.add(path)
        mime = _MIME.get(os.path.splitext(uri)[1].lower(), "application/octet-stream")
        with open(path, "rb") as bf:
            b64 = base64.b64encode(bf.read()).decode("ascii")
        return f"data:{mime};base64,{b64}"

    for buf in doc.get("buffers", []):
        if "uri" in buf:
            buf["uri"] = inline(buf["uri"])
    for img in doc.get("images", []):
        if "uri" in img:
            img["uri"] = inline(img["uri"])

    with open(gltf_path, "w") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    for p in sidecars:
        os.remove(p)


def export_open_scene(out_path: str, kwargs: dict) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    bpy.ops.export_scene.gltf(filepath=out_path, **kwargs)
    _embed_gltf(out_path)
    print(f"  -> {out_path}")


def main() -> None:
    src, out = _parse(_argv_after_ddash())
    kwargs = _supported_kwargs()

    if src:  # batch: open each .blend fresh, then export
        blends = sorted(f for f in os.listdir(src) if f.lower().endswith(".blend"))
        if not blends:
            sys.exit(f"No .blend files in {src}")
        print(f"Exporting {len(blends)} characters from {src} -> {out}")
        for name in blends:
            stem = os.path.splitext(name)[0]
            print(f"[{stem}]")
            bpy.ops.wm.open_mainfile(filepath=os.path.join(src, name))
            export_open_scene(os.path.join(out, stem + ".gltf"), kwargs)
    else:  # single file already opened by the Blender CLI
        opened = bpy.data.filepath
        if not opened:
            sys.exit("No .blend opened and no --src given. See the docstring.")
        stem = os.path.splitext(os.path.basename(opened))[0]
        print(f"[{stem}]")
        export_open_scene(os.path.join(out, stem + ".gltf"), kwargs)

    print("Done.")


if __name__ == "__main__":
    main()

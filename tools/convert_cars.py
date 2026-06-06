"""Convert the Realistic Car Pack OBJ models to GLB the game can load.

raylib/pyray segfaults on some .obj/.mtl, so (like the trees) we bake each car to
a .glb with trimesh first. Drop the pack's OBJ folder into assets/cars/source/
(the .obj + .mtl + any textures), then run:

    .venv/bin/python tools/convert_cars.py

Each Source/<Name>.obj becomes assets/cars/<Name>.glb, recentred on the origin in
x/z and sitting on y=0, so the in-game loader only has to scale it. Textures
referenced by the .mtl are embedded into the .glb.

FBX note: trimesh can't read .fbx reliably and raylib can't load it at all, so use
the OBJ versions (this pack ships both). If you only had FBX, convert via Blender
(`File > Export > glTF`) or assimp instead.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "assets", "cars", "source")
OUT = os.path.join(ROOT, "assets", "cars")


# Lift the pack's dark baked Kd colours so they read under the park's flat/unlit
# rendering (raylib can't push a material colour past 255, so we brighten here).
COLOR_GAIN = 1.7
TEX_GAIN = 1.12       # gentler lift for atlas-sampled colours (already vivid)
_IMG_EXT = (".png", ".jpg", ".jpeg", ".tga", ".bmp")

# Several pack models exported with every material flattened to the same grey
# (Kd 0.64 ≈ 163) and no UVs/texture — the real colour only survives in their
# .blend. But the part NAMES are semantic, so we repaint by name to match the
# pack's preview. Keyword match on the part name first, else the model's body.
_PAINT = {
    "windows": (150, 210, 232), "window": (150, 210, 232), "light": (248, 228, 140),
    "bumper": (140, 146, 152), "wheel": (32, 32, 36), "detail": (44, 48, 58),
    "border": (232, 232, 228), "handle": (44, 48, 58), "black": (32, 32, 34),
    "white": (236, 236, 230), "grey": (150, 154, 160), "gray": (150, 154, 160),
    "red": (200, 52, 46), "yellow": (240, 205, 55), "green": (70, 180, 80),
    "orange": (242, 120, 40), "blue": (74, 140, 210),
}
_BODY = {
    "Bus": (74, 140, 210), "SchoolBus": (242, 205, 55), "Ambulance": (238, 238, 232),
    "Train": (200, 52, 46), "Bicycle": (200, 52, 46), "SquareFrameBicycle": (200, 52, 46),
    "TrafficSign1": (200, 52, 46), "TrafficSign2": (200, 52, 46),
    "TrafficSign3": (200, 52, 46), "TrafficLight": (46, 50, 56),
    "TrafficCone": (242, 120, 40), "Taxi": (240, 205, 55),
}
_BODY_PARTS = {"top", "bottom", "outside", "sign", "bike", "frame"}


def _named_color(model: str, part: str):
    """Colour for a named part: the housing/body first (so 'TrafficLight' isn't
    mistaken for a 'light'), then keyword match, else the model's body colour."""
    p = part.lower()
    body = _BODY.get(model)
    if p in ("trafficlight", "pole", "base") or p.startswith("material") or p in _BODY_PARTS:
        return body
    for kw, col in _PAINT.items():
        if kw in p:
            return col
    return body


def _is_grey_default(col) -> bool:
    """True for the pack's flat ~163 grey that means 'colour was lost on export'."""
    return col is not None and col[0] == col[1] == col[2] and 150 <= col[0] <= 176


def _find_atlas(name: str):
    """The texture for a UV-mapped model: a per-model image (source/<name>.png)
    if present, else the single shared atlas image dropped in source/."""
    for ext in _IMG_EXT:
        p = os.path.join(SRC, name + ext)
        if os.path.exists(p):
            return p
    imgs = [f for f in sorted(os.listdir(SRC)) if f.lower().endswith(_IMG_EXT)]
    return os.path.join(SRC, imgs[0]) if imgs else None


def _sample_atlas(uv, atlas_path):
    """Sample an (N,2) UV array against the atlas image → (N,3) uint8 colours."""
    import numpy as np
    from PIL import Image

    arr = np.asarray(Image.open(atlas_path).convert("RGB"))
    h, w = arr.shape[:2]
    u = np.clip((uv[:, 0] % 1.0) * (w - 1), 0, w - 1).astype(int)
    v = np.clip((1.0 - (uv[:, 1] % 1.0)) * (h - 1), 0, h - 1).astype(int)   # flip V
    cols = arr[v, u].astype(float) * TEX_GAIN
    return np.clip(cols, 0, 255).astype("uint8")


def convert_one(trimesh, name: str, in_path: str) -> bool:
    """Bake colours into VERTEX colours and merge to one mesh — raylib renders
    those reliably (the per-material/texture baseColor otherwise comes through
    white in-game). UV-mapped parts sample the atlas texture (the real multi-
    colour paint); plain parts use the .mtl Kd. Recentres on origin, sits on y=0."""
    import numpy as np

    try:
        scene = trimesh.load(in_path, force="scene")
        atlas = _find_atlas(name)
        parts, textured, painted = [], False, False
        for gkey, geom in scene.geometry.items():
            uv = getattr(getattr(geom, "visual", None), "uv", None)
            try:
                kd = list(geom.visual.material.main_color)
            except Exception:
                kd = None
            part = gkey[len(name) + 1:] if gkey.startswith(name + "_") else gkey

            if atlas is not None and uv is not None and len(uv) == len(geom.vertices):
                rgb = _sample_atlas(np.asarray(uv), atlas)          # atlas texture
                vcols = np.c_[rgb, np.full(len(rgb), 255, "uint8")]
                textured = True
            elif _is_grey_default(kd) and _named_color(name, part) is not None:
                col = np.array(list(_named_color(name, part)) + [255], dtype="uint8")
                vcols = np.tile(col, (len(geom.vertices), 1))       # repaint by name
                painted = True
            else:
                col = np.array(kd if kd else [200, 200, 200, 255], dtype=float)
                col[:3] = np.clip(col[:3] * COLOR_GAIN, 0, 255)     # real Kd, brightened
                vcols = np.tile(col.astype("uint8"), (len(geom.vertices), 1))
            geom.visual = trimesh.visual.ColorVisuals(geom, vertex_colors=vcols)
            parts.append(geom)
        mesh = trimesh.util.concatenate(parts) if len(parts) > 1 else parts[0]
        lo, hi = mesh.bounds
        mesh.apply_translation([-(lo[0] + hi[0]) / 2.0, -lo[1], -(lo[2] + hi[2]) / 2.0])
        out_path = os.path.join(OUT, name + ".glb")
        mesh.export(out_path)
        tag = " [textured]" if textured else (" [painted]" if painted else "")
        print(f"  ✓ {name}.glb  ({os.path.getsize(out_path) // 1024} KB){tag}")
        return True
    except Exception as exc:
        print(f"  ✗ {name}: {exc}")
        return False


def main() -> int:
    global SRC, OUT
    # Optional: `convert_cars.py <src_obj_dir> <out_glb_dir>` to convert any pack
    # (e.g. the street tiles in assets/city/OBJ → assets/city/glb).
    if len(sys.argv) >= 3:
        SRC, OUT = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    try:
        import trimesh
    except ImportError:
        print("trimesh not installed. Run: .venv/bin/pip install trimesh")
        return 2
    if not os.path.isdir(SRC):
        print(f"Source folder not found: {SRC}")
        print("Copy the pack's OBJ folder there first (see this file's docstring).")
        return 2
    os.makedirs(OUT, exist_ok=True)
    objs = sorted(f for f in os.listdir(SRC) if f.lower().endswith(".obj"))
    if not objs:
        print(f"No .obj files in {SRC}")
        return 2
    print(f"Converting {len(objs)} car(s) → {OUT}")
    ok = 0
    for f in objs:
        name = os.path.splitext(f)[0]
        if convert_one(trimesh, name, os.path.join(SRC, f)):
            ok += 1
    print(f"Done: {ok}/{len(objs)} converted.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

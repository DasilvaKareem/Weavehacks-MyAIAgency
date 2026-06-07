"""Model + animation loading and caching.

raylib loads .gltf/.glb/.obj/.iqm/.vox/.m3d natively (NOT .fbx — convert those to
.glb/.gltf with Blender first). The Kenney .gltf files are self-contained (mesh,
textures and animations all embedded). Missing files fall back to placeholder
geometry so the game always runs.
"""
from __future__ import annotations

import json
import os
import pyray as pr

from . import skinning

MODELS_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "assets", "models")


class ModelRegistry:
    """Lazy-loads and caches models + their animations. Use after init_window()."""

    def __init__(self) -> None:
        self._models: dict[str, object] = {}
        self._anims: dict[str, tuple] = {}   # filename -> (anim_ptr, count)
        self._anim_index: dict[tuple, int] = {}  # (filename, name) -> clip index
        self._mat_index: dict[tuple, int] = {}    # (filename, name) -> material index
        self._mapped: set[str] = set()  # files whose material map is built (from pristine colors)
        self._missing: set[str] = set()
        self._hair_mesh: dict[str, int] = {}   # filename -> mesh index of its "Hair" mesh
        self._skin_shader = None  # lazily built on first model load (needs GL ctx)
        # Day/night reaches characters as a single draw tint (the shader's lighting
        # uniforms wouldn't upload on this build); folded into colDiffuse at draw.
        self.char_tint = pr.WHITE

    def _path(self, filename: str) -> str:
        return os.path.abspath(os.path.join(MODELS_DIR, filename))

    def get(self, filename: str | None):
        """Return a loaded raylib Model, or None to signal placeholder fallback."""
        if not filename:
            return None
        if filename in self._models:
            return self._models[filename]
        if filename in self._missing:
            return None
        path = self._path(filename)
        if not os.path.exists(path):
            self._missing.add(filename)
            return None
        model = pr.load_model(path)
        # Drive animation on the GPU so skinning doesn't explode the mesh.
        if self._skin_shader is None:
            self._skin_shader = skinning.load_skinning_shader()
        skinning.apply_to_model(model, self._skin_shader)
        self._models[filename] = model
        # Build the material name->index map NOW, while the model's diffuse colors
        # are still pristine. Per-character tinting mutates those colors at draw, so
        # if the map were (re)built later it would match names against tinted colors
        # and mis-assign — collapsing every NPC to one wrong tint. Build once, here.
        self._build_material_map(filename)
        return self._models[filename]

    def get_animations(self, filename: str | None):
        """Return (anim_array, count) for a model, or (None, 0) if none/missing."""
        if not filename or filename in self._missing:
            return None, 0
        if filename in self._anims:
            return self._anims[filename]
        path = self._path(filename)
        if not os.path.exists(path):
            return None, 0
        count = pr.ffi.new("int *")
        anims = pr.load_model_animations(path, count)
        self._anims[filename] = (anims, count[0])
        return self._anims[filename]

    def get_anim_index(self, filename: str | None, name: str) -> int:
        """Index of the animation clip called `name` (cached); 0 if not found."""
        key = (filename, name)
        if key in self._anim_index:
            return self._anim_index[key]
        anims, count = self.get_animations(filename)
        idx = 0
        for i in range(count):
            nm = pr.ffi.string(anims[i].name).decode() if anims[i].name else ""
            if nm == name:
                idx = i
                break
        self._anim_index[key] = idx
        return idx

    def get_material_index(self, filename: str | None, name: str) -> int:
        """Real raylib index of the material called `name` (cached); -1 if absent.

        pyray doesn't expose material names, so we read them (and their base
        colors) from the glTF JSON, then match each to the LOADED model's
        materials by diffuse color. This is robust to raylib's loader inserting
        an extra default material (which shifts positional indices) — naive
        "JSON order == raylib order" is off by one and tints the wrong surface.
        Only works for text .gltf (ours); .glb falls back to -1 (no tint).
        """
        if not filename:
            return -1
        if filename not in self._mapped:
            self.get(filename)            # loads the model + builds the map (pristine)
            if filename not in self._mapped:
                return -1                 # model missing / not a text .gltf
        # Cache the MISS too: a name this model lacks (e.g. the "Black" suit material
        # on a casual NPC, or "Hair" on a bald one) is recorded as -1 so it's never
        # re-derived. Never rebuild the map here — that would read tinted colors.
        key = (filename, name)
        if key not in self._mat_index:
            self._mat_index[key] = -1
        return self._mat_index[key]

    def _build_material_map(self, filename: str) -> None:
        """Map every named glTF material of `filename` to its real raylib index
        by nearest diffuse color, claiming each raylib slot at most once. Built
        exactly once per file (from get(), on load) while colors are pristine —
        idempotent so a stray call after tinting can't corrupt it."""
        if filename in self._mapped:
            return
        model = self._models.get(filename)
        try:
            with open(self._path(filename)) as fh:
                gltf_mats = json.load(fh).get("materials", [])
        except (OSError, ValueError):
            gltf_mats = []
        if model is None or not gltf_mats:
            for mat in gltf_mats:
                self._mat_index.setdefault((filename, mat.get("name")), -1)
            if model is not None:
                self._mapped.add(filename)   # .glb / no-materials: mapped, all misses
            return

        n = model.materialCount
        ray = []
        for i in range(n):
            c = model.materials[i].maps[pr.MATERIAL_MAP_DIFFUSE].color
            ray.append((c.r, c.g, c.b))
        used: set[int] = set()
        for mat in gltf_mats:
            nm = mat.get("name")
            bcf = mat.get("pbrMetallicRoughness", {}).get("baseColorFactor", [1, 1, 1, 1])
            exp = tuple(round(x * 255) for x in bcf[:3])
            best, best_d = -1, 1 << 30
            for i in range(n):
                if i in used:
                    continue
                d = sum((ray[i][k] - exp[k]) ** 2 for k in range(3))
                if d < best_d:
                    best, best_d = i, d
            if best >= 0:
                used.add(best)
            self._mat_index[(filename, nm)] = best
        self._mapped.add(filename)

    def hair_mesh_index(self, filename: str | None) -> int:
        """Mesh index whose material is named "Hair" (-1 if the model has none, e.g.
        a bald character). Lets a character hide its built-in hair or borrow another
        model's hair mesh as a hairstyle. Read from the glTF JSON once, then cached."""
        if not filename:
            return -1
        if filename in self._hair_mesh:
            return self._hair_mesh[filename]
        model = self.get(filename)
        idx = -1
        if model is not None:
            try:
                with open(self._path(filename)) as fh:
                    names = [m.get("name") for m in json.load(fh).get("materials", [])]
                for i in range(model.meshCount):
                    mi = model.meshMaterial[i]   # 1-based: raylib inserts a default material at 0
                    if 0 < mi <= len(names) and names[mi - 1] == "Hair":
                        idx = i
                        break
            except (OSError, ValueError):
                idx = -1
        self._hair_mesh[filename] = idx
        return idx

    def set_daylight(self, cycle) -> None:
        """No-op for characters: the day/night cycle still drives the sky, but its
        tint is no longer baked onto character models (the draw-tint approximation
        looked off in daylight, so characters stay un-tinted / WHITE)."""
        self.char_tint = pr.WHITE

    def unload_all(self) -> None:
        for anims, count in self._anims.values():
            pr.unload_model_animations(anims, count)
        for model in self._models.values():
            pr.unload_model(model)
        self._models.clear()
        self._anims.clear()

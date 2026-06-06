"""Game entities: the CEO and hired agent characters.

Characters render real Kenney models (assets/models/*.gltf) and play their Idle
animation. Missing models fall back to a colored box. Later, `backend_id` links
an on-screen agent to its LangGraph/Gemini worker.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
import pyray as pr

from . import config


@dataclass
class Character:
    name: str
    role: str
    x: float
    z: float
    color: pr.Color           # fallback color + label accent
    dept: str = ""            # department, shown on the label
    skin_tone: pr.Color | None = None  # tint for the "Skin" material
    hair_tone: pr.Color | None = None  # tint for the "Hair" material
    outfit_tone: pr.Color | None = None  # tint for the suit jacket ("Black") material
    eye_tone: pr.Color | None = None  # tint for the eyes (the "Face" material)
    hair_style: int = 0       # index into config.HAIRSTYLES (own hair / bald / borrowed)
    yaw: float = 0.0          # facing, degrees about Y
    y: float = 0.0            # vertical offset (for jumping)
    model: str | None = None  # filename in assets/models/, or None
    desk: tuple | None = None  # world (x, z) of this character's desk, if any
    backend_id: str | None = None
    status: str = "idle"      # idle | working | done
    anim_name: str = config.ANIM_IDLE_NAME
    anim_loop: bool = True    # False => play once and hold the last frame (e.g. SitDown)
    brain: object = None      # BotBrain driving autonomous movement (agents only)
    seat: tuple | None = None  # world (x, z) of this character's chair, if seated work
    home_room: str | None = None  # interior room key this agent belongs to (its wing)
    _frame: float = -1.0      # <0 => seed a per-character phase on first update

    @property
    def height(self) -> float:
        return config.CHARACTER_NATIVE_HEIGHT * config.CHARACTER_SCALE

    @property
    def label_anchor(self) -> pr.Vector3:
        return pr.Vector3(self.x, self.y + self.height + 0.4, self.z)

    def update(self, dt: float, registry) -> None:
        # Only advance this character's playback clock here. The actual bone pose
        # is applied in draw(), right before this character renders, so several
        # characters sharing one cached model each get their own pose instead of
        # all snapping to whoever updated last.
        _anims, count = registry.get_animations(self.model)
        if count <= 0:
            return
        if self._frame < 0:                       # desync idles across characters
            self._frame = (abs(self.x) + abs(self.z)) * 7.0
        self._frame += dt * config.ANIM_FPS

    def draw(self, registry) -> None:
        model = registry.get(self.model)
        pos = pr.Vector3(self.x, self.y, self.z)
        if model is not None:
            frame = 0
            anims, count = registry.get_animations(self.model)
            if count > 0:
                clip = anims[registry.get_anim_index(self.model, self.anim_name)]
                fc = max(1, clip.frameCount)
                if self.anim_loop:
                    frame = int(max(self._frame, 0.0)) % fc
                else:
                    # Play once, then hold the final pose (e.g. sit down and stay).
                    frame = min(int(max(self._frame, 0.0)), fc - 1)
                # GPU skinning: upload this character's bone matrices for the shader.
                pr.update_model_animation_bones(model, clip, frame)

            # Per-character material tints: set the shared model's flat-colored
            # materials just before drawing (immediate mode => stays per-character).
            # Skin spans two materials (hands "Skin" + face "Face"), so tint both.
            for mat in config.SKIN_MATERIAL_NAMES:
                self._tint(registry, model, mat, self.skin_tone)
            self._tint(registry, model, config.HAIR_MATERIAL_NAME, self.hair_tone)
            self._tint(registry, model, config.SUIT_MATERIAL_NAME, self.outfit_tone)
            self._tint(registry, model, config.EYE_MATERIAL_NAME, self.eye_tone)

            # Hairstyle: keep the model's own hair (Default) or hide it and, unless
            # bald, draw a hair mesh borrowed from another model over the head.
            hair_src = self._hair_source()
            self._set_own_hair_alpha(registry, model, 255 if hair_src is None else 0)

            s = config.CHARACTER_SCALE
            tint = getattr(registry, "char_tint", pr.WHITE)  # time-of-day lighting
            pr.draw_model_ex(model, pos, pr.Vector3(0, 1, 0), self.yaw,
                             pr.Vector3(s, s, s), tint)
            if hair_src and hair_src != "bald":
                self._draw_borrowed_hair(registry, hair_src, frame, s, tint)
        else:
            body = pr.Vector3(self.x, self.height / 2.0, self.z)
            pr.draw_cube(body, 0.6, self.height, 0.6, self.color)
            pr.draw_cube_wires(body, 0.6, self.height, 0.6, pr.BLACK)

    def _hair_source(self) -> str | None:
        """The source-model file for the chosen hairstyle: None for the model's own
        hair, "bald" for none, else a filename to borrow a hair mesh from."""
        styles = config.HAIRSTYLES
        if 0 <= self.hair_style < len(styles):
            return styles[self.hair_style][1]
        return None

    def _set_own_hair_alpha(self, registry, model, alpha: int) -> None:
        """Show (255) or hide (0) the model's built-in hair mesh via its material
        alpha — so a borrowed hairstyle replaces it instead of poking through."""
        mi = registry.get_material_index(self.model, config.HAIR_MATERIAL_NAME)
        if mi >= 0:
            model.materials[mi].maps[pr.MATERIAL_MAP_DIFFUSE].color.a = alpha

    def _draw_borrowed_hair(self, registry, src: str, frame: int, s: float, tint) -> None:
        """Pose another model's skeleton to our current frame and draw just its hair
        mesh over our head (the rig is shared, so it lands and animates correctly)."""
        hmodel = registry.get(src)
        hidx = registry.hair_mesh_index(src)
        if hmodel is None or hidx < 0:
            return
        hanims, hcount = registry.get_animations(src)
        if hcount > 0:
            hclip = hanims[registry.get_anim_index(src, self.anim_name)]
            pr.update_model_animation_bones(hmodel, hclip, frame % max(1, hclip.frameCount))
        mat = hmodel.materials[hmodel.meshMaterial[hidx]]
        base = self.hair_tone or pr.Color(84, 54, 32, 255)
        mat.maps[pr.MATERIAL_MAP_DIFFUSE].color = _mul_color(base, tint)
        xf = pr.matrix_multiply(
            pr.matrix_multiply(pr.matrix_scale(s, s, s),
                               pr.matrix_rotate_y(math.radians(self.yaw))),
            pr.matrix_translate(self.x, self.y, self.z))
        pr.draw_mesh(hmodel.meshes[hidx], mat, xf)

    def _tint(self, registry, model, material_name: str, color) -> None:
        """Tint one named flat material on the (shared) model, if it exists."""
        if color is None:
            return
        mi = registry.get_material_index(self.model, material_name)
        if mi >= 0:
            model.materials[mi].maps[pr.MATERIAL_MAP_DIFFUSE].color = color


def _rgba(col):
    """Normalize a color given as a pyray Color OR a plain (r,g,b[,a]) tuple
    (pyray exposes constants like WHITE as tuples) into (r, g, b, a)."""
    if hasattr(col, "r"):
        return col.r, col.g, col.b, col.a
    return col[0], col[1], col[2], (col[3] if len(col) > 3 else 255)


def _mul_color(c, tint) -> pr.Color:
    """Multiply a base color by a tint (both 0-255), preserving the base alpha.
    Used so a borrowed hair mesh — drawn with draw_mesh, which has no tint arg —
    still picks up the time-of-day dimming the body gets via draw_model_ex."""
    cr, cg, cb, ca = _rgba(c)
    tr, tg, tb, _ = _rgba(tint)
    return pr.Color(cr * tr // 255, cg * tg // 255, cb * tb // 255, ca)


def make_ceo(col: float, row: float, model: str) -> Character:
    x, z = config.grid_to_world(col, row)
    return Character(name="You (CEO)", role="CEO", x=x, z=z,
                     color=pr.GOLD, yaw=0.0, model=model)

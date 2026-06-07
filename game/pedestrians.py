"""GTA-style ambient pedestrians wandering the park sidewalks.

How it works (the same trick open-world games use — "ped streaming"):
  * We keep a small fixed pool of ~12 people, NOT the whole city's population.
  * Each walks the sidewalk graph: nodes are the four corners of every road
    intersection; edges run along a block's edge (a clean stretch of sidewalk) or
    across a road at an intersection (a street crossing). A pedestrian walks to a
    neighbouring corner, then picks another — so it follows sidewalks and turns at
    corners instead of cutting through roads or buildings. Now and then it pauses.
  * Streaming: when someone wanders too far from the CEO we recycle them — respawn
    at a fresh corner just out of view near the CEO. So there are always a few
    people around you, cheaply, without simulating thousands.

Rendering/animation reuse the normal Character pipeline (Walk/Idle clips); this
module only drives positions, so it stays light. Park mode only.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from . import config, locomotion, roster
from .entities import Character
from .park import BLOCK, AVENUES, STREETS, CENTER, ROAD_W

import pyray as pr

# A mix of everyday models so the crowd looks varied (not the fantasy ones).
PED_MODELS = [
    "Casual_Male.gltf", "Casual_Female.gltf", "Casual2_Male.gltf",
    "Casual2_Female.gltf", "Casual3_Male.gltf", "Casual3_Female.gltf",
    "Worker_Male.gltf", "Worker_Female.gltf", "OldClassy_Male.gltf",
    "OldClassy_Female.gltf", "Casual_Bald.gltf",
]
PED_COLOR = pr.Color(120, 124, 132, 255)     # fallback box tint if a model misses

SIDEWALK_OFF = ROAD_W / 2 + 1.8              # corner offset from a road centre
NEAR_R = 14.0                                 # don't pop in right on top of the CEO
SPAWN_R = 55.0                                # spawn within this radius of the CEO
DESPAWN_R = 78.0                              # recycle once further than this


@dataclass
class _Ped:
    ch: Character
    node: tuple        # current corner (a, s, cx, cz), cx/cz in {0,1}
    prev: tuple        # last corner (so we don't immediately backtrack)
    target: tuple      # corner we're walking toward
    tx: float
    tz: float
    speed: float
    idle: float = 0.0  # seconds left standing still


class Pedestrians:
    def __init__(self, count: int = 12, seed: int = 11) -> None:
        self.count = count
        self.rng = random.Random(seed)
        self.off = SIDEWALK_OFF
        self.peds: list[_Ped] = []

    # --- sidewalk graph ----------------------------------------------------

    def _world(self, node) -> tuple[float, float]:
        a, s, cx, cz = node
        x = (a - CENTER + 0.5) * BLOCK + (self.off if cx else -self.off)
        z = (s - CENTER + 0.5) * BLOCK + (self.off if cz else -self.off)
        return x, z

    def _neighbors(self, node) -> list:
        """Corners reachable on foot: along the block edge in x and z (no road
        crossed), and the two corners across the road at this intersection."""
        a, s, cx, cz = node
        out = [
            (a + 1, s, 0, cz) if cx == 1 else (a - 1, s, 1, cz),   # block edge in x
            (a, s + 1, cx, 0) if cz == 1 else (a, s - 1, cx, 1),   # block edge in z
            (a, s, 1 - cx, cz),                                    # cross the avenue
            (a, s, cx, 1 - cz),                                    # cross the street
        ]
        return [(A, S, CX, CZ) for (A, S, CX, CZ) in out
                if 1 <= A <= AVENUES - 1 and 1 <= S <= STREETS - 1]

    def _retarget(self, ped: _Ped) -> None:
        opts = [n for n in self._neighbors(ped.node) if n != ped.prev] \
            or self._neighbors(ped.node)
        ped.prev = ped.node
        ped.target = self.rng.choice(opts)
        ped.tx, ped.tz = self._world(ped.target)
        if self.rng.random() < 0.12:           # dawdle at the corner sometimes
            ped.idle = self.rng.uniform(0.8, 2.6)

    # --- spawning / streaming ---------------------------------------------

    def _node_near(self, px: float, pz: float):
        ap = min(AVENUES - 1, max(1, round(px / BLOCK + CENTER - 0.5)))
        sp = min(STREETS - 1, max(1, round(pz / BLOCK + CENTER - 0.5)))
        node = (ap, sp, 0, 0)
        for _ in range(16):
            a = min(AVENUES - 1, max(1, ap + self.rng.randint(-4, 4)))
            s = min(STREETS - 1, max(1, sp + self.rng.randint(-4, 4)))
            cand = (a, s, self.rng.randint(0, 1), self.rng.randint(0, 1))
            x, z = self._world(cand)
            if NEAR_R <= math.hypot(x - px, z - pz) <= SPAWN_R:
                return cand
        return node

    def _make(self, px: float, pz: float) -> _Ped:
        node = self._node_near(px, pz)
        x, z = self._world(node)
        ch = Character(name="", role="", x=x, z=z, color=PED_COLOR,
                       model=self.rng.choice(PED_MODELS))
        # Give each walker a real skin/hair/eye color, else the model's raw "Skin"
        # material (~black) shows. Keep each model's own hair (hair_style 0) so the
        # ambient crowd costs no extra draw calls.
        roster.apply_look(ch, {
            "skin_idx": self.rng.randrange(len(config.SKIN_TONES)),
            "hair_idx": self.rng.randrange(len(config.HAIR_COLORS)),
            "eye_idx": self.rng.randrange(len(config.EYE_COLORS)),
        })
        ch.yaw = self.rng.uniform(0.0, 360.0)
        ped = _Ped(ch=ch, node=node, prev=node, target=node, tx=x, tz=z,
                   speed=self.rng.uniform(1.3, 2.0))
        self._retarget(ped)
        return ped

    def _respawn(self, ped: _Ped, px: float, pz: float) -> None:
        node = self._node_near(px, pz)
        ped.node = ped.prev = node
        ped.ch.x, ped.ch.z = self._world(node)
        self._retarget(ped)

    # --- per-frame ---------------------------------------------------------

    def update(self, dt: float, px: float, pz: float, registry) -> None:
        if not self.peds:
            self.peds = [self._make(px, pz) for _ in range(self.count)]
        for ped in self.peds:
            if (ped.ch.x - px) ** 2 + (ped.ch.z - pz) ** 2 > DESPAWN_R * DESPAWN_R:
                self._respawn(ped, px, pz)
            self._step(ped, dt)
            ped.ch.update(dt, registry)        # advance its Walk/Idle clock

    def _step(self, ped: _Ped, dt: float) -> None:
        if ped.idle > 0:
            ped.idle -= dt
            locomotion.apply_anim(ped.ch, moving=False)
            return
        if locomotion.move_toward(ped.ch, ped.tx, ped.tz, ped.speed, dt):
            ped.node = ped.target
            self._retarget(ped)
        locomotion.apply_anim(ped.ch, moving=ped.idle <= 0)

    def draw(self, registry) -> None:
        for ped in self.peds:
            ped.ch.draw(registry)

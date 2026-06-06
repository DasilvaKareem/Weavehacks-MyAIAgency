"""Ambient city traffic: a population of cars that drive the park's road grid.

The park (game/park.py) is a 20x20 grid of blocks with two-lane asphalt roads on
every block boundary. This module models cars driving that grid: each car runs
along a road from one intersection to the next, keeps to the right-hand lane, and
picks a new direction at each intersection (preferring to go straight, never an
immediate U-turn, turning back at the edges).

It's deliberately raylib-free so it stays unit-testable; the park owns the render
(picks a GLB per `car.model`, falls back to an oriented box). Drives in park mode
only — the park advances it each frame and draws `traffic.cars`.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .park import BLOCK, AVENUES, STREETS, CENTER, ROAD_W

# Road centre-lines, matching park._draw_streets: avenue road `a` is the vertical
# road at this x; street road `s` is the horizontal road at this z.
def ave_x(a: int) -> float:
    return (a - CENTER + 0.5) * BLOCK


def st_z(s: int) -> float:
    return (s - CENTER + 0.5) * BLOCK


# Intersections live on a (1..AVENUES-1) x (1..STREETS-1) lattice.
I_MIN, I_MAX = 1, AVENUES - 1
J_MIN, J_MAX = 1, STREETS - 1

DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1)]
HALF_LANE = ROAD_W * 0.25          # keep-right offset from the centre line


@dataclass(frozen=True)
class Vehicle:
    """One drivable model. `yaw` aligns the model's nose to +z (cars are already
    +z, the buses are modelled along +x so they need 90). `box` is the
    length/width/height for the no-model fallback; `weight` is spawn frequency.

    `tint`: some pack models are the colour-per-material variant (real baked
    colours → draw white-tinted), others are the texture-atlas variant whose PNG
    we don't have (they bake to ~white → give them a flat tint so they're not
    grey). None means draw with no tint."""
    name: str            # GLB basename in assets/cars
    yaw: float           # extra yaw so the nose points along travel
    smin: float
    smax: float
    box: tuple           # (length, width, height) for the box fallback
    color: tuple         # fallback body colour
    weight: int
    tint: tuple | None = None   # flat tint for atlas-variant models


# The road fleet. Train (too long to turn) and the bikes (distorted proportions
# in this pack) are intentionally left out. The first five carry real per-colour
# materials (tint=None); the rest are atlas models we tint flat.
VEHICLES = [
    Vehicle("NormalCar1", 0.0, 6.0, 8.5, (4.2, 1.8, 1.3), (210, 60, 55), 5),
    Vehicle("NormalCar2", 0.0, 6.0, 8.5, (3.3, 1.6, 1.2), (40, 90, 180), 5),
    Vehicle("SportsCar", 0.0, 8.0, 11.0, (4.0, 1.8, 1.2), (235, 200, 70), 3),
    Vehicle("SportsCar2", 0.0, 8.0, 11.0, (3.9, 1.9, 1.2), (235, 235, 240), 3),
    Vehicle("Cop", 0.0, 7.0, 10.0, (3.7, 1.8, 1.2), (40, 44, 52), 2),
    Vehicle("Taxi", 0.0, 6.0, 8.0, (3.8, 1.8, 1.4), (235, 200, 40), 3),
    Vehicle("Ambulance", 0.0, 6.5, 9.0, (5.7, 2.4, 2.6), (236, 236, 240), 1),
    Vehicle("Bus", 90.0, 4.5, 6.0, (4.1, 1.7, 1.7), (70, 120, 200), 1),
    Vehicle("SchoolBus", 90.0, 4.5, 6.0, (4.6, 1.9, 2.2), (235, 190, 40), 1),
]
_VWEIGHTS = [v.weight for v in VEHICLES]


@dataclass
class Car:
    i: int                 # node we're driving FROM (avenue idx, street idx)
    j: int
    dx: int                # heading on the grid (one of DIRS)
    dz: int
    vtype: int             # index into VEHICLES
    speed: float           # world units / sec
    x: float = 0.0         # world position (centre-line + lane offset), set live
    z: float = 0.0
    yaw: float = 0.0       # facing, degrees about Y
    _seg: float = 0.0      # distance travelled along the current block


class Traffic:
    def __init__(self, count: int = 30, seed: int = 7) -> None:
        self.rng = random.Random(seed)
        self.cars: list[Car] = [self._spawn() for _ in range(count)]
        for c in self.cars:           # place them on-road immediately
            self._place(c, 0.0)

    # --- spawning ----------------------------------------------------------

    def _spawn(self) -> Car:
        r = self.rng
        # Start on a random road, heading along it, with the first node we drive
        # toward guaranteed in-bounds (the heading constrains the start cell).
        if r.random() < 0.5:                            # avenue road i (vertical): N/S
            i = r.randint(I_MIN, I_MAX)
            dx, dz = 0, r.choice((1, -1))
            j = r.randint(J_MIN, J_MAX - 1) if dz == 1 else r.randint(J_MIN + 1, J_MAX)
        else:                                           # street road j (horizontal): E/W
            j = r.randint(J_MIN, J_MAX)
            dx, dz = r.choice((1, -1)), 0
            i = r.randint(I_MIN, I_MAX - 1) if dx == 1 else r.randint(I_MIN + 1, I_MAX)
        vt = r.choices(range(len(VEHICLES)), weights=_VWEIGHTS, k=1)[0]
        v = VEHICLES[vt]
        return Car(i=i, j=j, dx=dx, dz=dz, vtype=vt, speed=r.uniform(v.smin, v.smax))

    # --- per-frame ---------------------------------------------------------

    def update(self, dt: float) -> None:
        for c in self.cars:
            c._seg += c.speed * dt
            while c._seg >= BLOCK:        # crossed into the next intersection
                c._seg -= BLOCK
                c.i += c.dx
                c.j += c.dz
                self._turn(c)
            self._place(c, c._seg)

    def _turn(self, c: Car) -> None:
        """Choose the heading out of the node we just reached."""
        back = (-c.dx, -c.dz)
        choices, weights = [], []
        for d in DIRS:
            if d == back:
                continue
            ni, nj = c.i + d[0], c.j + d[1]
            if I_MIN <= ni <= I_MAX and J_MIN <= nj <= J_MAX:
                choices.append(d)
                weights.append(3 if d == (c.dx, c.dz) else 1)   # prefer straight
        if not choices:                  # dead corner: U-turn is the only way out
            choices, weights = [back], [1]
        c.dx, c.dz = self.rng.choices(choices, weights=weights, k=1)[0]

    def _place(self, c: Car, seg: float) -> None:
        """World position = point `seg` along the block from node (i,j) in the
        heading, nudged to the right-hand lane; yaw faces the heading."""
        ax, az = ave_x(c.i), st_z(c.j)
        fx = ax + c.dx * seg
        fz = az + c.dz * seg
        # Right-hand lane offset: perpendicular-right of travel = (dz, -dx).
        c.x = fx + c.dz * HALF_LANE
        c.z = fz - c.dx * HALF_LANE
        c.yaw = math.degrees(math.atan2(c.dx, c.dz))

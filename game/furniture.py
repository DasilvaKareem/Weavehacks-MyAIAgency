"""Procedurally generated office furniture, built from raylib primitives.

The character pack ships no environment props, so the office is dressed with
parametric furniture assembled from cubes / cylinders / spheres: chairs at every
desk plus a seeded scatter of plants, filing cabinets, a water cooler, bins,
rugs and a lounge couch around the room's perimeter.

Determinism matters: all variation (sizes, colours, variants) is resolved ONCE
when the layout is generated and baked into each `Prop`, so draw() is pure and
the office looks identical every frame and every launch (for a fixed seed).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import pyray as pr

from . import config


def _c(rgb, a: int = 255) -> pr.Color:
    return pr.Color(int(rgb[0]), int(rgb[1]), int(rgb[2]), a)


def _jitter(rng: random.Random, base, amt: int) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(v + rng.uniform(-amt, amt)))) for v in base)


# Palettes
CHAIR_SEATS = [(40, 44, 52), (60, 70, 90), (120, 60, 60), (40, 90, 80), (70, 60, 100)]
FRAME = (30, 32, 38)
POTS = [(150, 95, 70), (90, 92, 98), (60, 62, 70), (180, 170, 150)]
LEAVES = [(46, 130, 70), (60, 150, 80), (38, 110, 60), (70, 160, 90), (44, 120, 64)]
CABINET = [(150, 152, 160), (90, 100, 120), (70, 72, 80), (120, 110, 95)]
RUGS = [(180, 70, 70), (70, 110, 170), (90, 150, 120), (170, 150, 90), (120, 100, 160)]


@dataclass
class Prop:
    kind: str
    x: float
    z: float
    p: dict = field(default_factory=dict)


# --- per-prop builders (bake randomness) + drawers (pure) -------------------

def _draw_chair(x: float, z: float, p: dict) -> None:
    seat_h = p["seat_h"]
    face = p["face"]                       # +1 backrest at -z, -1 at +z
    frame = _c(FRAME)
    # 5-spoke star base (offsets baked at layout time)
    for ox, oz in p["spokes"]:
        pr.draw_cube(pr.Vector3(x + ox, 0.05, z + oz), 0.07, 0.06, 0.07, frame)
    pr.draw_cylinder(pr.Vector3(x, 0.02, z), 0.05, 0.06, seat_h - 0.06, 8, frame)  # post
    pr.draw_cube(pr.Vector3(x, seat_h, z), 0.5, 0.1, 0.5, _c(p["seat"]))           # seat
    pr.draw_cube(pr.Vector3(x, seat_h + 0.3, z + face * 0.22), 0.5, 0.5, 0.08, _c(p["seat"]))  # back


def make_chair(rng: random.Random, x: float, z: float, face: int = 1) -> Prop:
    spokes = [(0.20, 0.0), (-0.20, 0.0), (0.0, 0.20), (0.0, -0.20), (0.14, 0.14)]
    return Prop("chair", x, z, {
        "seat": _jitter(rng, rng.choice(CHAIR_SEATS), 10),
        "seat_h": rng.uniform(0.42, 0.5),
        "face": face,
        "spokes": spokes,
    })


def _draw_plant(x: float, z: float, p: dict) -> None:
    pot_h = p["pot_h"]
    pr.draw_cylinder(pr.Vector3(x, 0.0, z), p["pot_top"], p["pot_bot"], pot_h, 10, _c(p["pot"]))
    pr.draw_cylinder_wires(pr.Vector3(x, 0.0, z), p["pot_top"], p["pot_bot"], pot_h, 10,
                           _c((0, 0, 0), 60))
    for (oy, r, col) in p["blobs"]:
        pr.draw_sphere(pr.Vector3(x, pot_h + oy, z), r, _c(col))


def make_plant(rng: random.Random, x: float, z: float, tall: bool | None = None) -> Prop:
    tall = rng.random() < 0.45 if tall is None else tall
    pot = _jitter(rng, rng.choice(POTS), 12)
    if tall:
        pot_h = rng.uniform(0.45, 0.7)
        base = rng.uniform(0.7, 1.1)
        blobs = [(base * f, rng.uniform(0.34, 0.46), _jitter(rng, rng.choice(LEAVES), 14))
                 for f in (0.0, 0.5, 1.0)]
        pot_top, pot_bot = 0.26, 0.32
    else:
        pot_h = rng.uniform(0.18, 0.26)
        blobs = [(rng.uniform(0.1, 0.2), rng.uniform(0.22, 0.32),
                  _jitter(rng, rng.choice(LEAVES), 14)) for _ in range(rng.randint(1, 2))]
        pot_top, pot_bot = 0.2, 0.16
    return Prop("plant", x, z, {"pot": pot, "pot_h": pot_h, "pot_top": pot_top,
                                "pot_bot": pot_bot, "blobs": blobs})


def _draw_cabinet(x: float, z: float, p: dict) -> None:
    n, body = p["drawers"], _c(p["body"])
    dh = 0.34
    total = n * dh
    for i in range(n):
        cy = dh / 2 + i * dh
        pr.draw_cube(pr.Vector3(x, cy, z), 0.8, dh - 0.03, 0.6, body)
        pr.draw_cube_wires(pr.Vector3(x, cy, z), 0.8, dh - 0.03, 0.6, _c((0, 0, 0), 70))
        pr.draw_cube(pr.Vector3(x, cy, z + 0.31), 0.24, 0.05, 0.03, _c((30, 32, 38)))  # handle
    pr.draw_cube(pr.Vector3(x, total + 0.02, z), 0.84, 0.04, 0.64, _c(p["top"]))       # lid


def make_cabinet(rng: random.Random, x: float, z: float) -> Prop:
    body = _jitter(rng, rng.choice(CABINET), 12)
    return Prop("cabinet", x, z, {
        "drawers": rng.randint(2, 4), "body": body,
        "top": _jitter(rng, body, 8),
    })


def _draw_cooler(x: float, z: float, p: dict) -> None:
    pr.draw_cube(pr.Vector3(x, 0.45, z), 0.4, 0.9, 0.4, _c((230, 235, 240)))         # stand
    pr.draw_cube_wires(pr.Vector3(x, 0.45, z), 0.4, 0.9, 0.4, _c((0, 0, 0), 50))
    pr.draw_cylinder(pr.Vector3(x, 0.9, z), 0.18, 0.2, 0.5, 12, _c((90, 160, 210, 200)))  # bottle
    pr.draw_sphere(pr.Vector3(x, 1.45, z), 0.16, _c((120, 180, 220, 200)))


def make_cooler(rng: random.Random, x: float, z: float) -> Prop:
    return Prop("cooler", x, z, {})


def _draw_bin(x: float, z: float, p: dict) -> None:
    pr.draw_cylinder(pr.Vector3(x, 0.0, z), 0.16, 0.13, 0.42, 10, _c(p["body"]))
    pr.draw_cylinder_wires(pr.Vector3(x, 0.0, z), 0.17, 0.14, 0.43, 10, _c((0, 0, 0), 60))


def make_bin(rng: random.Random, x: float, z: float) -> Prop:
    return Prop("bin", x, z, {"body": _jitter(rng, (70, 75, 85), 15)})


def _draw_rug(x: float, z: float, p: dict) -> None:
    w, d = p["w"], p["d"]
    pr.draw_cube(pr.Vector3(x, 0.012, z), w, 0.02, d, _c(p["color"]))
    pr.draw_cube(pr.Vector3(x, 0.014, z), w * 0.8, 0.021, d * 0.8, _c(p["inner"]))


def make_rug(rng: random.Random, x: float, z: float) -> Prop:
    color = rng.choice(RUGS)
    return Prop("rug", x, z, {
        "w": rng.uniform(2.2, 3.4), "d": rng.uniform(1.6, 2.4),
        "color": color, "inner": _jitter(rng, color, 30),
    })


def _draw_couch(x: float, z: float, p: dict) -> None:
    body = _c(p["body"])
    w = p["w"]
    pr.draw_cube(pr.Vector3(x, 0.22, z), w, 0.28, 0.9, body)             # seat base
    pr.draw_cube(pr.Vector3(x, 0.45, z - 0.34), w, 0.5, 0.22, body)      # backrest
    for ax in (-w / 2 + 0.12, w / 2 - 0.12):                            # armrests
        pr.draw_cube(pr.Vector3(x + ax, 0.4, z), 0.22, 0.36, 0.9, _c(p["arm"]))


def make_couch(rng: random.Random, x: float, z: float) -> Prop:
    body = rng.choice([(70, 90, 130), (110, 70, 80), (80, 100, 90), (90, 88, 100)])
    return Prop("couch", x, z, {"body": _jitter(rng, body, 12),
                                "arm": _jitter(rng, body, 30), "w": rng.uniform(2.2, 2.8)})


_DRAW = {
    "chair": _draw_chair, "plant": _draw_plant, "cabinet": _draw_cabinet,
    "cooler": _draw_cooler, "bin": _draw_bin, "rug": _draw_rug, "couch": _draw_couch,
}


def draw_prop(prop: Prop) -> None:
    _DRAW[prop.kind](prop.x, prop.z, prop.p)


# Build any catalog item by kind (used by the shop). `params` come from the JSON.
_BUILDERS = {
    "chair": lambda rng, x, z, p: make_chair(rng, x, z, p.get("face", 1)),
    "plant": lambda rng, x, z, p: make_plant(rng, x, z, p.get("tall")),
    "cabinet": lambda rng, x, z, p: make_cabinet(rng, x, z),
    "cooler": lambda rng, x, z, p: make_cooler(rng, x, z),
    "bin": lambda rng, x, z, p: make_bin(rng, x, z),
    "rug": lambda rng, x, z, p: make_rug(rng, x, z),
    "couch": lambda rng, x, z, p: make_couch(rng, x, z),
}


def build(kind: str, rng: random.Random, x: float, z: float, params: dict | None = None) -> Prop:
    return _BUILDERS[kind](rng, x, z, params or {})


# A chair tucked at a desk — seeded by position so it's stable without a layout.
# `face` orients the backrest (+1 backrest at -z, -1 at +z) so a seated worker
# faces their desk.
def draw_desk_chair(x: float, z: float, face: int = 1) -> None:
    rng = random.Random(hash((round(x, 2), round(z, 2))) & 0xFFFFFFFF)
    draw_prop(make_chair(rng, x, z, face=face))


# A lounge cluster (rug + couch) at a plan's lounge zone. Seeded by position so
# it's stable. The couch backrest is at -z, so a seated person faces +z — which
# is the facing the game assigns to the lounge seat.
def draw_lounge(x: float, z: float) -> None:
    rng = random.Random(hash((round(x, 2), round(z, 2), "lounge")) & 0xFFFFFFFF)
    draw_prop(make_rug(rng, x, z + 0.1))
    draw_prop(make_couch(rng, x, z))


# --- layout generation ------------------------------------------------------

def generate_layout(seed: int = config.FURNITURE_SEED,
                    cols: int | None = None, rows: int | None = None) -> list[Prop]:
    """Seeded perimeter scatter of ambient decor (plants/cabinets/cooler/bins),
    sized to the floor plan. Lounges/couches are NOT placed here — they're drawn
    from the plan's lounge zones (see Scene) so they line up with where bots sit.
    The room has no collision, so exact spacing is purely cosmetic."""
    rng = random.Random(seed)
    w = (cols if cols is not None else config.GRID_COLS) * config.TILE
    d = (rows if rows is not None else config.GRID_ROWS) * config.TILE
    hx, hz = w / 2, d / 2
    props: list[Prop] = []

    # Four corner statement plants.
    for sx in (-1, 1):
        for sz in (-1, 1):
            props.append(make_plant(rng, sx * (hx - 0.8), sz * (hz - 0.8), tall=True))

    # Back-wall strip (behind the desks): cabinets, cooler, plants, bins.
    back_z = -hz + 0.7
    n_back = max(4, int(w / 2.6))
    for i in range(n_back):
        t = (i + 0.5) / n_back
        x = -hx + 1.2 + t * (w - 2.4) + rng.uniform(-0.3, 0.3)
        kind = rng.choices(["cabinet", "plant", "cooler", "bin"],
                            weights=[4, 3, 1, 2])[0]
        props.append(_make(kind, rng, x, back_z + rng.uniform(-0.15, 0.15)))

    # Side-wall strips.
    for side in (-1, 1):
        sx = side * (hx - 0.7)
        n_side = max(3, int(d / 2.6))
        for i in range(n_side):
            t = (i + 0.5) / n_side
            z = -hz + 2.0 + t * (d - 4.0) + rng.uniform(-0.3, 0.3)
            kind = rng.choices(["plant", "cabinet", "bin"], weights=[5, 2, 2])[0]
            props.append(_make(kind, rng, sx, z))

    return props


def _make(kind: str, rng: random.Random, x: float, z: float) -> Prop:
    return {
        "cabinet": make_cabinet, "plant": make_plant, "cooler": make_cooler,
        "bin": make_bin,
    }[kind](rng, x, z)

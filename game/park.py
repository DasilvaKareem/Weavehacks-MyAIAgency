"""Walkable 3D office park: lease buildings (deposit + rent) and enter them.

The park is the "outside" world (mode == 'park' in main.py): a plaza with a grid
of building lots. Some are leased (yours, enterable); the rest advertise a LEASE
sign and a deposit + monthly rent. Buildings are primitives — base + roof + door
+ window grid + a dept-coloured sign band — so they always render (the imported
OBJ building pack can swap in later). Leasing is deposit-once + rent-per-month;
rent accrues on a slow timer regardless of which mode you're in.
"""
from __future__ import annotations

import json
import math
import os
import random
from dataclasses import dataclass

import pyray as pr

from . import businesses, config
from .season import SEASON_SUFFIX, SEASON_TINT
from .terrain import Terrain

# Backdrop buildings (every converted model) used to fill the skyline around the
# six playable lots. Drawn full-colour, no interaction.
SCENERY_MODELS = [
    "1Story.glb", "1Story_GableRoof.glb", "1Story_RoundRoof.glb", "1Story_Sign.glb",
    "2Story.glb", "2Story_2.glb", "2Story_Balcony.glb", "2Story_Columns.glb",
    "2Story_Double.glb", "2Story_GableRoof.glb", "2Story_RoundRoof.glb",
    "2Story_Sidehouse.glb", "2Story_Sign.glb", "2Story_Slim.glb", "2Story_Stairs.glb",
    "2Story_Wide.glb", "2Story_Wide_2Doors.glb", "3Story_Balcony.glb", "3Story_Slim.glb",
    "3Story_Small.glb", "4Story.glb", "4Story_Wide_2Doors.glb", "6Story_Stack.glb",
]
SCENERY_SEED = 7

LOTS_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "assets", "park_lots.json")

# --- City grid: 1st..20th Avenue (x) by 1st..20th Street (z) ----------------
AVENUES = 20
STREETS = 20
BLOCK = 16.0          # pitch between addresses (wide blocks = roomy streets)
CENTER = 10.5         # address that maps to world origin-ish (centres the grid)
ROAD_W = 6.5          # asphalt width between blocks (two-lane road)
TARGET_W = 5.5        # buildings scaled to ~this wide (~15% smaller than before)
SIGN_W = 3.0          # themed shop signs are scaled to about this wide
REACH = 3.6           # how close the CEO must stand to interact
CULL_DIST = 62.0      # only draw backdrop buildings within this of the camera
BUILDINGS_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "assets", "buildings", "glb")
NATURE_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "assets", "nature")

# Tree families we scatter as street trees. Each has a base (summer/green),
# an _Autumn and a _Snow (winter) variant, numbered _1.._5 (see assets/nature).
TREE_FAMILIES = ["CommonTree", "BirchTree", "PineTree"]
TREE_TARGET_H = 4.6     # every tree scaled to ~this tall, then jittered per tree
TREE_LIGHT_GAIN = 2.6   # brighten the pack's dark baked diffuse (drawn flat, unlit)

MONTH_SECONDS = 60.0   # one in-game "month" of real time per rent charge
DESKS_PER_LEASE = 3    # capacity unlocked per leased office

# Downtown addresses (avenue, street) for the playable lots + flavor shops. The
# rest of the 20x20 grid is filled with backdrop buildings.
LOT_ADDR = {"growth": (9, 10), "hq": (10, 10), "finance": (11, 10),
            "eng": (9, 11), "research": (10, 11), "design": (11, 11),
            # The affordable starter office (unlocked by meeting Mae in the park).
            "starter": (12, 10)}
# Spread the shops across the whole city so you find them while exploring.
NPC_ADDR = {"barbershop": (4, 6), "hardware": (5, 14), "grocery": (7, 4),
            "bookshop": (6, 17), "cafe": (13, 5), "convenience": (15, 16),
            "pharmacy": (16, 8), "casino": (17, 13), "bakery": (3, 11),
            # Quest-stop civic buildings, clustered near downtown so you bump into
            # them early (when their to-dos and seed-cash rewards matter most).
            "chamber": (8, 8), "registrar": (12, 8), "research_firm": (8, 12),
            "bureau": (12, 12), "signshop": (13, 9), "brandstudio": (7, 10),
            "citybank": (13, 11), "broker": (14, 11),
            # The Grants Office (LLM-judged business grants), civic cluster.
            "grants": (8, 10),
            # The Startup Incubator hosts the Business Model Canvas workshop.
            "incubator": (10, 7),
            # Apex Ventures (the VC firm) on its own downtown block in the cluster.
            "ventures": (12, 9),
            # The Angel Investor (Apex's seed desk) — one block north of spawn, on the
            # walk into downtown, clear of the parks so you find it first.
            "angel": (10, 8),
            # The Trade Embassy — runs the idle South-America farm (passive income).
            "embassy": (9, 9),
            # Storefronts: The Outfitters by the brand studio; staffing up north.
            "outfitters": (7, 9), "staffing": (11, 7)}

GROUND = pr.Color(176, 178, 184, 255)     # sidewalk concrete
ASPHALT = pr.Color(64, 66, 72, 255)       # road
LANE = pr.Color(210, 200, 120, 255)       # centre-line dashes
ROOF = pr.Color(58, 62, 72, 255)
WINDOW = pr.Color(150, 200, 235, 255)
SIGN_LEASE = pr.Color(220, 80, 70, 255)   # red "for lease" band


def block_pos(ave: float, st: float) -> tuple[float, float]:
    """World (x, z) for a grid address (avenue, street)."""
    return ((ave - CENTER) * BLOCK, (st - CENTER) * BLOCK)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def address_label(px: float, pz: float) -> str:
    """Nearest 'Nth Ave & Mth St' for a world position."""
    a = min(AVENUES, max(1, round(px / BLOCK + CENTER)))
    s = min(STREETS, max(1, round(pz / BLOCK + CENTER)))
    return f"{_ordinal(a)} Ave & {_ordinal(s)} St"


def load_lots(path: str = LOTS_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["lots"]


def load_npc(path: str = LOTS_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("npc", [])


def load_parks(path: str = LOTS_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("parks", [])


@dataclass
class NpcBuilding:
    """A non-leasable flavor shop (casino, bakery, …). `sign` is an optional GLB
    word-sign mounted over the door; `awning` tints the door canopy.

    When `task` is set the building is a QUEST STOP: walking up and pressing E
    completes that tasks.py to-do, pays `reward` seed cash once, and posts `blurb`
    to the inbox. A `tasks` LIST instead makes it a WORKSHOP that steps through
    several to-dos one visit at a time (e.g. the Business Model Canvas). When
    `store` is set the building is a STOREFRONT (e.g. "outfit" → the wardrobe shop):
    walking up and pressing E opens that store. Plain flavor shops leave all empty."""
    id: str
    name: str
    model: str
    sign: str | None
    x: float
    z: float
    awning: tuple
    task: str | None = None
    reward: int = 0
    blurb: str = ""
    tasks: tuple = ()          # workshop: an ordered list of to-do keys
    store: str | None = None   # storefront kind ("outfit" = the wardrobe shop)
    market: str | None = None  # idle-market venue ("bank" / "broker") opened inside
    service: str | None = None # civic service opened inside ("grant" = the grants office)
    game: str | None = None    # arcade game opened inside ("slots" = the casino's slot machine)
    plan: str | None = None    # interior floor-plan id (else the default quest plan)

    @property
    def is_quest_stop(self) -> bool:
        return self.task is not None or bool(self.tasks)

    @property
    def is_game(self) -> bool:
        return self.game is not None

    @property
    def is_store(self) -> bool:
        return self.store is not None

    @property
    def is_market(self) -> bool:
        return self.market is not None

    @property
    def is_service(self) -> bool:
        return self.service is not None

    @property
    def interactive(self) -> bool:
        """Walk-up + E does something here (a quest, storefront, market, service, or game)."""
        return (self.is_quest_stop or self.is_store or self.is_market
                or self.is_service or self.is_game)

    def task_keys(self) -> tuple:
        """Every to-do this stop can complete (one for a shop, many for a workshop)."""
        return (self.task,) if self.task else tuple(self.tasks)

    def pending(self, done: set) -> list:
        """Its to-do keys not yet completed, in order."""
        return [k for k in self.task_keys() if k not in done]

    def is_complete(self, done: set) -> bool:
        return not self.pending(done)


@dataclass
class GreenSpace:
    """A named city park: a decorative green block that stands in place of a
    backdrop building at its grid address. Walkable, non-interactive — grass +
    scattered seasonal trees (added to the shared tree list) + a central fountain
    + a floating name label (drawn by the overlay in main.py)."""
    id: str
    name: str
    x: float
    z: float


# Offset from a building centre onto its sidewalk for street trees: the band runs
# from the footprint half (~2.75) to the road's inner edge (~4.65), so ~3.7 sits
# mid-sidewalk, clear of both the wall and the asphalt.
SIDEWALK_OFF = 3.7

# City-park look. The lawn must fit WITHIN its block: the surrounding roads run at
# ±8 from the centre with an inner edge ~4.65 in, so a half of ~4.3 keeps the lawn
# (and its grove) on the lot, off the asphalt, with a thin sidewalk margin. Season
# recolours the lawn the way SEASON_SUFFIX recolours trees (green/brown/snow).
PARK_HALF = 4.3        # half-size of the square lawn (fits the block, clear of roads)
PARK_GRASS = {"Summer": (104, 156, 84), "Autumn": (158, 142, 80),
              "Winter": (226, 230, 236), "Dead": (132, 118, 90)}
PARK_PATH = (190, 184, 168)        # crushed-stone path crossing the lawn
PARK_KERB = (150, 152, 158)        # stone fountain basin + kerb
PARK_WATER = {"Summer": (120, 170, 205), "Autumn": (120, 170, 205),
              "Winter": (210, 224, 236), "Dead": (110, 130, 150)}


def _c(rgb, a: int = 255) -> pr.Color:
    return pr.Color(int(rgb[0]), int(rgb[1]), int(rgb[2]), a)


@dataclass
class Building:
    id: str
    name: str
    dept: str
    deposit: int
    rent: int
    model: str
    color: tuple
    x: float
    z: float
    status: str        # 'hq' | 'leased' | 'available'
    plan: str = "hq"   # floor-plan template id (single-room buildings)
    structure: dict | None = None  # optional floors/wings interior (see interior.py)
    locked: bool = False  # can't be leased until a gate is cleared (e.g. meet an NPC)

    @property
    def leased(self) -> bool:
        return self.status in ("hq", "leased")


@dataclass
class _Loaded:
    model: object
    scale: float
    y_off: float       # lift so the model's base sits on y=0
    half_d: float      # scaled half-depth (door offset)
    top: float         # scaled height (banner placement)


# Realistic facade palette. The converted models bake a single green theme into
# every building, so we repaint the green-dominant materials per building-type
# with one of these muted, real-world stone/brick tones (keeping the original
# lightness so windows, trim and shading survive).
CITY_PALETTE = [
    (190, 182, 166), (172, 150, 120), (150, 96, 78), (124, 128, 134), (140, 148, 160),
    (160, 128, 100), (198, 192, 180), (112, 102, 94), (176, 140, 110), (132, 140, 134),
    (200, 168, 130), (104, 112, 124),
]

HEIGHT_BOOST = 1.7     # default vertical stretch (used when no heightmap applies)

# --- Skyline heightmap: tall downtown core, falling off to low-rise outskirts,
# plus smooth noise. Returns a per-block vertical stretch multiplier. ----------
HMAP_BASE = 1.1        # minimum height (far outskirts)
HMAP_CORE = 1.4        # extra height at the downtown centre
HMAP_NOISE = 0.5       # rolling local variance


def _skyline(a: float, s: float) -> float:
    """Vertical-stretch multiplier for a building at grid address (a, s)."""
    dx = (a - CENTER) / (AVENUES * 0.5)
    dz = (s - CENTER) / (STREETS * 0.5)
    r = min(1.0, math.hypot(dx, dz))
    core = (1.0 - r) ** 1.5                                   # 1 at centre -> 0 at edge
    n = (math.sin(a * 0.8 + s * 0.4) + math.sin(a * 0.3 - s * 0.7)
         + math.sin((a + s) * 0.5)) / 3.0
    n = (n + 1.0) * 0.5                                       # 0..1
    return HMAP_BASE + core * HMAP_CORE + n * HMAP_NOISE


def _brighten(model, gain: float) -> None:
    """Scale every material's diffuse colour by `gain` (clamped). The nature pack
    bakes dark, lighting-dependent colours; we draw trees flat/unlit, so without
    this they read as near-black silhouettes."""
    for i in range(model.materialCount):
        c = model.materials[i].maps[pr.MATERIAL_MAP_DIFFUSE].color
        model.materials[i].maps[pr.MATERIAL_MAP_DIFFUSE].color = pr.Color(
            min(255, int(c.r * gain)), min(255, int(c.g * gain)),
            min(255, int(c.b * gain)), c.a)


def _recolor_walls(model, key: str) -> None:
    """Repaint every green-themed material with a stable per-key facade colour,
    preserving the original brightness so windows/trim/shading survive. Neutral
    greys (concrete, glass frames) are left untouched."""
    hue = CITY_PALETTE[sum(key.encode()) % len(CITY_PALETTE)]
    hl = max(1.0, 0.299 * hue[0] + 0.587 * hue[1] + 0.114 * hue[2])
    for i in range(model.materialCount):
        c = model.materials[i].maps[pr.MATERIAL_MAP_DIFFUSE].color
        is_grey = max(c.r, c.g, c.b) - min(c.r, c.g, c.b) < 12
        green_dominant = c.g >= c.r and c.g >= c.b and (c.r + c.g + c.b) > 12
        if green_dominant and not is_grey:            # part of the baked green theme
            lum = 0.299 * c.r + 0.587 * c.g + 0.114 * c.b
            f = lum / hl                              # match this material's brightness
            model.materials[i].maps[pr.MATERIAL_MAP_DIFFUSE].color = pr.Color(
                min(255, int(hue[0] * f)), min(255, int(hue[1] * f)), min(255, int(hue[2] * f)), 255)


class _BuildingModels:
    """Loads + caches the scaled GLB building models (needs a GL context)."""

    def __init__(self) -> None:
        self._cache: dict[str, _Loaded | None] = {}

    def get(self, model_file: str) -> "_Loaded | None":
        if model_file in self._cache:
            return self._cache[model_file]
        path = os.path.abspath(os.path.join(BUILDINGS_DIR, model_file))
        if not os.path.exists(path):
            self._cache[model_file] = None
            return None
        m = pr.load_model(path)
        if "signs/" not in model_file:                 # leave the gold word-signs alone
            _recolor_walls(m, model_file)
        bb = pr.get_model_bounding_box(m)
        w = max(0.01, bb.max.x - bb.min.x)
        scale = TARGET_W / w
        self._cache[model_file] = _Loaded(
            model=m, scale=scale, y_off=-bb.min.y * scale,
            half_d=(bb.max.z - bb.min.z) * scale / 2.0,
            top=(bb.max.y - bb.min.y) * scale,
        )
        return self._cache[model_file]

    def unload(self) -> None:
        for ld in self._cache.values():
            if ld is not None:
                pr.unload_model(ld.model)
        self._cache.clear()


class _TreeModels:
    """Loads + caches the OBJ nature-pack trees, scaled to a target height. The
    pack bakes vertex colours into each .mtl, so these need no textures and no
    skinning shader — raylib loads .obj/.mtl natively. Only the variants for the
    seasons actually shown ever get loaded (lazy + cached per filename)."""

    def __init__(self) -> None:
        self._cache: dict[str, _Loaded | None] = {}

    def get(self, model_file: str) -> "_Loaded | None":
        if model_file in self._cache:
            return self._cache[model_file]
        path = os.path.abspath(os.path.join(NATURE_DIR, model_file))
        if not os.path.exists(path):
            self._cache[model_file] = None
            return None
        m = pr.load_model(path)
        _brighten(m, TREE_LIGHT_GAIN)
        bb = pr.get_model_bounding_box(m)
        h = max(0.01, bb.max.y - bb.min.y)
        scale = TREE_TARGET_H / h
        self._cache[model_file] = _Loaded(
            model=m, scale=scale, y_off=-bb.min.y * scale,
            half_d=(bb.max.z - bb.min.z) * scale / 2.0,
            top=(bb.max.y - bb.min.y) * scale,
        )
        return self._cache[model_file]

    def unload(self) -> None:
        for ld in self._cache.values():
            if ld is not None:
                pr.unload_model(ld.model)
        self._cache.clear()


# Vehicle + street-prop GLBs (assets/cars/<Name>.glb, made by tools/convert_cars).
# The pack is roughly metre-scale, so models load at native size (a bus stays
# bigger than a car); a missing file falls back to a box / is skipped.
CARS_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "assets", "cars")
CITY_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "assets", "city", "glb")
VEHICLE_SCALE = 1.0     # pack units ≈ world metres; keep relative vehicle sizes
CAR_LIGHT_GAIN = 1.4    # lift the pack's dark baked Kd colours (drawn flat/unlit)

# Modular street tiles (Kenney-style, 2x2 each), placed at every road intersection
# so corners land on building addresses. The tile's road is only ~1/3 of its
# footprint, so a block-fit scale (BLOCK/2=6) gives a too-narrow ~4-wide road. We
# over-scale so the road reads wide (~5.4); tiles then overlap into neighbours,
# which we hide with a tiny per-tile height parity (adjacent tiles never share a
# plane → no z-fighting). Drawn thin in Y so it's a flat surface, not a slab.
STREET_TILE = "Street_4Way"
STREET_XZ = 10.0               # → road ≈ 0.67*10 ≈ 6.7 wide; footprint (20) covers the
                               # 16-unit block with a little overlap (parity hides it)
STREET_Y = 0.5                 # flatten the 0.25-thick slab
STREET_Y_OFF = -0.1            # sit just under y=0 so the surface is ~flat
STREET_PARITY = 0.03           # height stagger between adjacent (overlapping) tiles
GROUND_Y = -0.08               # base concrete plane, sunk below the road tiles to kill z-fight
BUILDING_SINK = 0.12           # bury building bases below the road so footprints don't z-fight
TILE_CULL = 62.0               # wider grid + bigger tiles; match the building cull
CITY_PROP_SCALE = 6.0          # street-pack props (keep poles a sane height)


class _ModelCache:
    """Loads + caches converted GLBs by basename at a fixed scale, brightened for
    the park's flat lighting. Returns None for anything not converted yet."""

    def __init__(self, scale: float = VEHICLE_SCALE, base: str = CARS_DIR) -> None:
        self._cache: dict[str, _Loaded | None] = {}
        self._scale = scale
        self._base = base

    def get(self, name: str) -> "_Loaded | None":
        if name in self._cache:
            return self._cache[name]
        path = os.path.abspath(os.path.join(self._base, name + ".glb"))
        if not os.path.exists(path):
            self._cache[name] = None
            return None
        m = pr.load_model(path)
        _brighten(m, CAR_LIGHT_GAIN)
        bb = pr.get_model_bounding_box(m)
        s = self._scale
        self._cache[name] = _Loaded(
            model=m, scale=s, y_off=-bb.min.y * s, half_d=0.0,
            top=(bb.max.y - bb.min.y) * s)
        return self._cache[name]

    def unload(self) -> None:
        for ld in self._cache.values():
            if ld is not None:
                pr.unload_model(ld.model)
        self._cache.clear()


class Park:
    def __init__(self, lots: list[dict], npc: list[dict] | None = None,
                 parks: list[dict] | None = None) -> None:
        self.buildings: list[Building] = []
        self.npc: list[NpcBuilding] = [
            NpcBuilding(id=n["id"], name=n["name"], model=n["model"], sign=n.get("sign"),
                        x=float(n["x"]), z=float(n["z"]), awning=tuple(n["awning"]),
                        task=n.get("task"), reward=int(n.get("reward", 0)),
                        blurb=n.get("blurb", ""), tasks=tuple(n.get("tasks", ())),
                        store=n.get("store"), market=n.get("market"),
                        service=n.get("service"), game=n.get("game"),
                        plan=n.get("plan"))
            for n in (npc if npc is not None else load_npc())
        ]
        for lot in lots:
            x, z = block_pos(*LOT_ADDR.get(lot["id"], (10, 10)))
            self.buildings.append(Building(
                id=lot["id"], name=lot["name"], dept=lot["dept"],
                deposit=lot["deposit"], rent=lot["rent"], model=lot["model"],
                color=tuple(lot["color"]), x=x, z=z, status=lot["status"],
                plan=lot.get("plan", "hq"), structure=lot.get("structure"),
                locked=lot.get("locked", False),
            ))
        # snap the NPC shops to their grid addresses too
        for n in self.npc:
            if n.id in NPC_ADDR:
                n.x, n.z = block_pos(*NPC_ADDR[n.id])
        # City parks: named green spaces at grid addresses. Each one's block is
        # reserved in _build_city so no backdrop building spawns under the lawn.
        src = parks if parks is not None else load_parks()
        self.parks: list[GreenSpace] = []
        self._park_addrs: set[tuple[int, int]] = set()
        for p in src:
            a, s = int(p["ave"]), int(p["st"])
            x, z = block_pos(a, s)
            self.parks.append(GreenSpace(id=p["id"], name=p["name"], x=x, z=z))
            self._park_addrs.add((a, s))
        self._models = _BuildingModels()
        self._tree_models = _TreeModels()
        self._car_models = _ModelCache()      # vehicle GLBs
        self._prop_models = _ModelCache()     # street-furniture GLBs
        self._street_models = _ModelCache(base=CITY_DIR)   # modular road tiles
        # Ambient traffic driving the road grid. Lazy import avoids a cycle
        # (traffic.py reads this module's grid constants).
        from .traffic import Traffic
        self.traffic = Traffic()
        self._props = self._build_props()     # static lights/signs/cones
        self.rent_timer = 0.0
        # 3D terrain: flat concrete basin holding the whole road grid (so the flat
        # road/sidewalk quads keep working), ramping up into grass/rock/snow hills
        # well beyond the playable bounds. Built lazily on first draw (needs GL).
        self.terrain = Terrain(span=1150.0, flat_radius=225.0, city_radius=162.0,
                               baseline=GROUND_Y, max_height=60.0, res=190, ramp=140.0,
                               sea_drop=11.0, city_amp=0.0)   # city dead-flat; keep hills + sea
        # CEO spawns one block south of HQ, facing the downtown blocks (+z).
        self.spawn = block_pos(10, 9)
        ext = (AVENUES - CENTER) * BLOCK + 2.0
        self.bounds = (ext, ext)
        self._city, self._trees = self._build_city()

    def _build_city(self):
        """Fill every grid block (minus the downtown lots/shops) with a backdrop
        building; scatter street trees. Heavily distance-culled at draw time."""
        rng = random.Random(SCENERY_SEED)
        reserved = set(LOT_ADDR.values()) | set(NPC_ADDR.values())
        reserved |= {(10, 9), (9, 9), (11, 9)}   # keep the spawn lane clear
        reserved |= self._park_addrs             # a park lawn, not a building, here
        city = []     # (model, x, z, yaw, scale_mul)
        self._biz: list[businesses.Business] = []   # one tenant per backdrop block
        for a in range(1, AVENUES + 1):
            for s in range(1, STREETS + 1):
                if (a, s) in reserved:
                    continue
                x, z = block_pos(a, s)
                x += rng.uniform(-0.3, 0.3)
                z += rng.uniform(-0.3, 0.3)
                yaw = rng.choice([0.0, 90.0, 180.0, 270.0])
                vboost = _skyline(a, s) * rng.uniform(0.9, 1.12)   # heightmap + jitter
                model = rng.choice(SCENERY_MODELS)
                city.append((model, x, z, yaw, rng.uniform(0.92, 1.1), vboost))
                # Give this backdrop block a real tenant so it can be walked up to
                # (drawing stays the cheap culled path; this list is only scanned on
                # interaction). A handful of blocks are hand-placed named landmarks.
                sub = businesses.LANDMARK_ADDR.get((a, s))
                self._biz.append(businesses.landmark(sub, model, x, z) if sub
                                 else businesses.generate(model, x, z))

        # Street trees line the SIDEWALKS, not the road. Roads run down the half-
        # address lines between blocks, so block_pos(a+0.5, s+0.5) is the middle of
        # an intersection — exactly where trees must NOT go. Instead offset from a
        # building centre toward one of its corners by SIDEWALK_OFF, landing in the
        # concrete band between the footprint (±2.75) and the road's inner edge
        # (~4.65). Each gets a fixed family/variant/orientation so it stays the same
        # tree as the season swaps only its foliage (summer -> autumn -> snow).
        trees = []    # (x, z, family, variant, yaw, scale_mul)
        for a in range(6, 15):
            for s in range(6, 15):
                if (a, s) in self._park_addrs:        # parks plant their own grove
                    continue
                if rng.random() < 0.5:
                    bx, bz = block_pos(a, s)
                    ox = rng.choice((-SIDEWALK_OFF, SIDEWALK_OFF))
                    oz = rng.choice((-SIDEWALK_OFF, SIDEWALK_OFF))
                    jx, jz = rng.uniform(-0.3, 0.3), rng.uniform(-0.3, 0.3)
                    trees.append((bx + ox + jx, bz + oz + jz,
                                  rng.choice(TREE_FAMILIES), rng.randint(1, 5),
                                  rng.uniform(0.0, 360.0), rng.uniform(0.8, 1.25)))

        # Plant a small grove around each park's edges (clear of the centre fountain
        # and the path cross), reusing the same seasonal tree pipeline as the street
        # trees so a park changes with the seasons too.
        for p in self.parks:
            edge = PARK_HALF - 1.4
            for ox, oz in ((-edge, -edge), (edge, -edge), (-edge, edge), (edge, edge),
                           (0.0, -edge), (0.0, edge)):
                jx, jz = rng.uniform(-0.6, 0.6), rng.uniform(-0.6, 0.6)
                trees.append((p.x + ox + jx, p.z + oz + jz,
                              rng.choice(TREE_FAMILIES), rng.randint(1, 5),
                              rng.uniform(0.0, 360.0), rng.uniform(0.9, 1.3)))
        return city, trees

    def height_mult(self, x: float, z: float) -> float:
        """Skyline vertical multiplier for a building at world (x, z)."""
        return _skyline(x / BLOCK + CENTER, z / BLOCK + CENTER)

    def ground_y(self, x: float, z: float) -> float:
        """Terrain height ABOVE the city baseline at (x, z) — add to the Y of anything
        drawn so it rides the terrain, with its original offset preserved. In the flat
        city basin this is 0 (a no-op), so objects sit exactly where they always did;
        out on the hills it lifts them onto the slope. Navigation stays X/Z only."""
        return self.terrain.height_at(x, z) - self.terrain.baseline

    def top_of(self, b) -> float:
        """World-space top of a building (for floating labels)."""
        ld = self._models.get(b.model)
        return (ld.top * self.height_mult(b.x, b.z) + 1.2) if ld else 6.0

    def door_of(self, b: Building) -> tuple[float, float]:
        """Front-of-building interaction point (uses the model's scaled depth)."""
        ld = self._models.get(b.model)
        half_d = ld.half_d if ld else 2.5
        return (b.x, b.z + half_d + 0.6)

    def collide(self, px: float, pz: float, r: float = 0.45) -> tuple[float, float]:
        """Push (px,pz) out of any building footprint so the CEO can't walk through
        them. Resolves along the axis of least penetration (AABB vs point+radius)."""
        for b in (*self.buildings, *self.npc):
            ld = self._models.get(b.model)
            hw = TARGET_W / 2.0 + r
            hd = (ld.half_d if ld else 2.5) + r
            dx, dz = px - b.x, pz - b.z
            if abs(dx) < hw and abs(dz) < hd:                # inside the footprint
                if hw - abs(dx) < hd - abs(dz):              # least-penetration axis
                    px = b.x + (hw if dx >= 0 else -hw)
                else:
                    pz = b.z + (hd if dz >= 0 else -hd)
        return px, pz

    def update(self, dt: float) -> None:
        """Advance ambient systems (traffic). Called each park frame."""
        self.traffic.update(dt)

    def unload(self) -> None:
        self._models.unload()
        self._tree_models.unload()
        self._car_models.unload()
        self._prop_models.unload()
        self._street_models.unload()
        if hasattr(self, "_unit_cube"):
            pr.unload_model(self._unit_cube)
            del self._unit_cube

    # --- queries -----------------------------------------------------------
    def leased(self) -> list[Building]:
        return [b for b in self.buildings if b.leased]

    def monthly_rent(self) -> int:
        return sum(b.rent for b in self.buildings if b.status == "leased")

    def nearest(self, px: float, pz: float) -> Building | None:
        """The office building whose footprint is within REACH of (px,pz) — from
        ANY side, so you can lease/enter by walking up to whichever wall you reach
        (the door isn't always on the side you approach from)."""
        best, best_d = None, REACH
        for b in self.buildings:
            ld = self._models.get(b.model)
            hw, hd = TARGET_W / 2.0, (ld.half_d if ld else 2.5)
            dx = max(abs(px - b.x) - hw, 0.0)        # distance to the footprint edge
            dz = max(abs(pz - b.z) - hd, 0.0)
            d = math.hypot(dx, dz)
            if d < best_d:
                best, best_d = b, d
        return best

    def nearest_npc(self, px: float, pz: float) -> NpcBuilding | None:
        """The INTERACTIVE NPC building within REACH of (px,pz), nearest first —
        a quest stop or a storefront. Plain flavor shops are ignored."""
        best, best_d = None, REACH
        for n in self.npc:
            if not n.interactive:
                continue
            ld = self._models.get(n.model)
            hw, hd = TARGET_W / 2.0, (ld.half_d if ld else 2.5)
            dx = max(abs(px - n.x) - hw, 0.0)
            dz = max(abs(pz - n.z) - hd, 0.0)
            d = math.hypot(dx, dz)
            if d < best_d:
                best, best_d = n, d
        return best

    def nearest_business(self, px: float, pz: float):
        """The backdrop tenant (generated Business) within REACH of (px,pz). Every
        block has one, so this is the fallback that makes the whole city walkable —
        checked only when no lease lot or interactive shop is in reach."""
        best, best_d = None, REACH
        hw = TARGET_W / 2.0
        for b in self._biz:
            dx = max(abs(px - b.x) - hw, 0.0)
            dz = max(abs(pz - b.z) - hw, 0.0)
            d = math.hypot(dx, dz)
            if d < best_d:
                best, best_d = b, d
        return best

    # --- economy -----------------------------------------------------------
    def lease(self, b: Building) -> None:
        b.status = "leased"

    def tick_rent(self, dt: float) -> int:
        """Advance the rent clock; return cash due this frame (0 most frames,
        the full monthly rent on the frame a month rolls over)."""
        if self.monthly_rent() <= 0:
            return 0
        self.rent_timer += dt
        if self.rent_timer >= MONTH_SECONDS:
            self.rent_timer -= MONTH_SECONDS
            return self.monthly_rent()
        return 0

    def rent_progress(self) -> float:
        return min(1.0, self.rent_timer / MONTH_SECONDS)

    # --- draw --------------------------------------------------------------
    def draw(self, camera, season: str = "Summer", quest_done: set | None = None) -> None:
        cx, cz = camera.target.x, camera.target.z       # cull around where we look
        done = quest_done or set()
        pr.begin_mode_3d(camera)
        self.terrain.draw()                      # 3D land: flat city basin + hills
        self._draw_streets(cx, cz)
        self._draw_parks(cx, cz, season)        # lawns sit under the trees/fountains
        self._draw_cars(cx, cz)
        self._draw_props(cx, cz)
        self._draw_city(cx, cz)
        self._draw_trees(cx, cz, season)
        for n in self.npc:
            self._draw_npc(n)
        for n in self.npc:                               # quest markers over the shops
            if n.is_quest_stop and not n.is_complete(done):
                self._draw_quest_marker(n)
        for b in self.buildings:
            self._draw_building(b)
        for b in self.buildings:                         # beacons drawn last (on top)
            self._draw_beacon(b)
        pr.end_mode_3d()

    def _draw_cars(self, cx: float, cz: float) -> None:
        """Draw the traffic population near the camera. A converted GLB if we have
        one for this vehicle, else an oriented box (headings are axis-aligned, so
        the box just swaps length/width — no rotation needed)."""
        from .traffic import VEHICLES
        cull = CULL_DIST * CULL_DIST
        for c in self.traffic.cars:
            if (c.x - cx) ** 2 + (c.z - cz) ** 2 > cull:
                continue
            v = VEHICLES[c.vtype]
            ld = self._car_models.get(v.name)
            gy = self.ground_y(c.x, c.z)        # ride the terrain (X/Z nav unchanged)
            if ld is not None:
                s = ld.scale
                tint = _c(v.tint) if v.tint else pr.WHITE
                pr.draw_model_ex(ld.model, pr.Vector3(c.x, ld.y_off + gy, c.z),
                                 pr.Vector3(0, 1, 0), c.yaw + v.yaw,
                                 pr.Vector3(s, s, s), tint)
            else:
                self._draw_car_box(c, v, gy)

    def draw_vehicle(self, name: str, x: float, z: float, yaw: float) -> bool:
        """Draw one free-standing car (the player's drivable, not ambient traffic)
        at an arbitrary world pose. Reuses the same GLB cache, brightening and flat
        tint as the traffic fleet so the CEO's ride matches the city. `yaw` follows
        the shared convention (0 = nose along +Z). Returns True if a GLB drew.

        Unlike the ambient box fallback (axis-aligned), a freely-steered car needs a
        rotated body, so the no-model path draws an oriented box via a unit cube
        transform rather than draw_cube."""
        from .traffic import VEHICLES
        v = next((vv for vv in VEHICLES if vv.name == name), None)
        gy = self.ground_y(x, z)
        ld = self._car_models.get(name)
        if ld is not None:
            s = ld.scale
            extra = v.yaw if v else 0.0
            tint = _c(v.tint) if (v and v.tint) else pr.WHITE
            pr.draw_model_ex(ld.model, pr.Vector3(x, ld.y_off + gy, z),
                             pr.Vector3(0, 1, 0), yaw + extra, pr.Vector3(s, s, s), tint)
            return True
        # Fallback: an oriented body box (rotate a unit cube model on the fly).
        L, W, H = (v.box if v else (4.0, 1.8, 1.3))
        col = _c(v.color) if v else pr.Color(210, 60, 55, 255)
        if not hasattr(self, "_unit_cube"):
            self._unit_cube = pr.load_model_from_mesh(pr.gen_mesh_cube(1.0, 1.0, 1.0))
        pr.draw_model_ex(self._unit_cube, pr.Vector3(x, gy + H * 0.45, z),
                         pr.Vector3(0, 1, 0), yaw, pr.Vector3(W, H * 0.9, L), col)
        return False

    def _draw_car_box(self, c, v, gy: float = 0.0) -> None:
        body, cabin = _c(v.color), _c(tuple(int(x * 0.6) for x in v.color))
        glass = pr.Color(140, 180, 210, 255)
        L, W, H = v.box
        horiz = c.dx != 0                       # E/W → length along x, else along z
        bx, bz = (L, W) if horiz else (W, L)
        pr.draw_cube(pr.Vector3(c.x, gy + H * 0.28, c.z), bx, H * 0.45, bz, body)      # chassis
        cx2, cz2 = (L * 0.5, W * 0.82) if horiz else (W * 0.82, L * 0.5)
        pr.draw_cube(pr.Vector3(c.x, gy + H * 0.62, c.z), cx2, H * 0.42, cz2, cabin)   # cabin
        pr.draw_cube(pr.Vector3(c.x, gy + H * 0.62, c.z), cx2 * 0.98, H * 0.30, cz2 * 0.98, glass)
        ox, oz = (L * 0.34, W * 0.5) if horiz else (W * 0.5, L * 0.34)
        for sx in (-ox, ox):
            for sz in (-oz, oz):
                pr.draw_cube(pr.Vector3(c.x + sx, gy + 0.16, c.z + sz), 0.5, 0.34, 0.5,
                             pr.Color(24, 24, 28, 255))

    # --- static street furniture ------------------------------------------

    def _build_props(self) -> list:
        """Deterministic street furniture from the street pack: streetlights
        lining the avenues, traffic lights at downtown intersections, signs on
        scattered corners, and a car-pack coned-off lane near HQ. Each entry is
        (source, glb_name, x, z, yaw, scale) — source 'city' or 'cars'."""
        rng = random.Random(99)
        off = 5.5                                      # onto the sidewalk past the wider road
        CS = CITY_PROP_SCALE

        def ax(a):
            return (a - CENTER + 0.5) * BLOCK

        def sz(s):
            return (s - CENTER + 0.5) * BLOCK

        def corner(a, s, cx, cz):                      # cx,cz in {-1,1}: which corner
            yaw = math.degrees(math.atan2(-cx, -cz))   # face the intersection
            return (ax(a) + cx * off, sz(s) + cz * off, yaw)

        props: list = []
        for a in range(2, AVENUES - 1, 2):             # streetlights line the grid
            for s in range(2, STREETS - 1, 2):
                x, z, _ = corner(a, s, 1, 1)
                props.append(("city", "Streetlight_Single", x, z, 0.0, CS))
        for a in range(6, 16):                         # traffic lights downtown
            for s in range(6, 16):
                if a % 3 == 0 and s % 3 == 0:
                    x, z, yaw = corner(a, s, 1, -1)
                    props.append(("city", "TrafficLight", x, z, yaw, CS))
        signs = ["Sign_Stop", "Sign_NoParking", "Sign_Triangle"]
        for a in range(4, 18):                         # signs on scattered corners
            for s in range(4, 18):
                if (a * 3 + s) % 11 == 0:
                    cx, cz = rng.choice([(1, 1), (-1, 1), (1, -1), (-1, -1)])
                    x, z, yaw = corner(a, s, cx, cz)
                    props.append(("city", rng.choice(signs), x, z, yaw, CS))
        for k in range(6):                             # a coned-off lane near HQ
            props.append(("cars", "TrafficCone",
                          ax(10) - ROAD_W * 0.25, sz(9) + k * 1.3 - 3.0, 0.0, 1.0))
        return props

    def _draw_props(self, cx: float, cz: float) -> None:
        cull = CULL_DIST * CULL_DIST
        caches = {"cars": self._prop_models, "city": self._street_models}
        for source, name, x, z, yaw, scl in self._props:
            if (x - cx) ** 2 + (z - cz) ** 2 > cull:
                continue
            ld = caches[source].get(name)
            if ld is None:
                continue
            pr.draw_model_ex(ld.model, pr.Vector3(x, ld.y_off * scl + self.ground_y(x, z), z),
                             pr.Vector3(0, 1, 0), yaw, pr.Vector3(scl, scl, scl), pr.WHITE)

    def _draw_quest_marker(self, n: NpcBuilding) -> None:
        """The quest indicator over an unfinished quest-stop building (cleared once
        it's done). Anchors the floating diamond just above the building's roof."""
        top = (self._models.get(n.model).top if self._models.get(n.model) else 5.0)
        head = min(top * self.height_mult(n.x, n.z), 6.0) + 1.4
        self.draw_quest_indicator(n.x, n.z, head)

    def draw_quest_indicator(self, x: float, z: float, head_y: float) -> None:
        """A cyan ground ring + floating "!" diamond at a world spot, so the city
        itself points you at your next objective. `head_y` is where the diamond
        floats. Shared by quest-stop buildings AND free-standing quest NPCs (e.g. the
        park intern) so every "go here next" cue looks identical — one source of truth."""
        col = pr.Color(90, 210, 230, 255)
        gy = self.ground_y(x, z)
        pr.draw_cylinder(pr.Vector3(x, gy + 0.05, z), 3.2, 3.2, 0.05, 26, col)
        pr.draw_cylinder_wires(pr.Vector3(x, gy + 0.07, z), 3.4, 3.4, 0.07, 26, col)
        # a bobbing "!" diamond marker (uses time for a gentle float)
        my = gy + head_y + 0.25 * math.sin(pr.get_time() * 2.2 + x)
        pr.draw_cylinder(pr.Vector3(x, my, z), 0.0, 0.42, 0.5, 4, col)        # spike down
        pr.draw_cylinder(pr.Vector3(x, my + 0.5, z), 0.42, 0.0, 0.5, 4, col)  # spike up

    def draw_guide_beacon(self, x: float, z: float, head_y: float = 7.5) -> None:
        """The 'go here' marker for the to-do you picked on your phone. Deliberately
        brighter, taller, and gold (vs. the cyan quest rings) so the one objective you
        chose stands out from every other quest stop — a pulsing ground ring, four
        corner posts, a soft beam of light you can spot across the city, and a bobbing
        chevron over the door."""
        t = pr.get_time()
        gold = pr.Color(255, 205, 90, 255)
        gy = self.ground_y(x, z)
        pulse = 3.7 + 0.5 * math.sin(t * 3.0)
        pr.draw_cylinder(pr.Vector3(x, gy + 0.06, z), pulse, pulse, 0.06, 30, gold)
        pr.draw_cylinder_wires(pr.Vector3(x, gy + 0.08, z), pulse + 0.25, pulse + 0.25, 0.08, 30, gold)
        beam = pr.Color(255, 215, 120, 70)                # a column of light, visible from afar
        pr.draw_cylinder(pr.Vector3(x, gy, z), 0.45, 0.7, head_y, 16, beam)
        for ox, oz in ((-2.7, -2.7), (2.7, -2.7), (-2.7, 2.7), (2.7, 2.7)):
            pr.draw_cylinder(pr.Vector3(x + ox, gy, z + oz), 0.14, 0.14, 3.0, 6, gold)
        my = gy + head_y + 0.3 * math.sin(t * 2.4 + x)    # a bobbing chevron pointing at the door
        pr.draw_cylinder(pr.Vector3(x, my, z), 0.0, 0.55, 0.6, 4, gold)        # spike down
        pr.draw_cylinder(pr.Vector3(x, my + 0.6, z), 0.55, 0.0, 0.6, 4, gold)  # spike up

    def _draw_beacon(self, b: Building) -> None:
        """Street-level marker for an office lot: a glowing ground ring around the
        base plus a marker pole + orb at the front door. HQ gold, leased green,
        available orange — so you can always find your buildings."""
        col = {"hq": (255, 212, 96), "leased": (96, 224, 136)}.get(b.status, (255, 150, 70))
        gy = self.ground_y(b.x, b.z)
        # a glowing ground pad around the base — visible at street level from any
        # side (a sky beam would be hidden behind these tall buildings).
        pr.draw_cylinder(pr.Vector3(b.x, gy + 0.05, b.z), 3.7, 3.7, 0.06, 28, _c(col))
        pr.draw_cylinder_wires(pr.Vector3(b.x, gy + 0.07, b.z), 3.9, 3.9, 0.08, 28, _c(col))
        # corner posts so it reads even when you're right up against the building
        for ox, oz in ((-2.6, -2.6), (2.6, -2.6), (-2.6, 2.6), (2.6, 2.6)):
            pr.draw_cylinder(pr.Vector3(b.x + ox, gy, b.z + oz), 0.14, 0.14, 3.2, 6, _c(col))

    def _draw_streets(self, cx: float = 0.0, cz: float = 0.0) -> None:
        span = (AVENUES + 1) * BLOCK
        # (The flat base concrete is now the terrain's flat basin — see self.terrain,
        # drawn in draw(). It renders concrete-grey within city_radius at GROUND_Y.)
        # Modular road tiles: a 4-way at every intersection on the block grid,
        # scaled so one tile fills a block (its corners meet at the building
        # addresses). Falls back to flat asphalt strips if the tile isn't there.
        ld = self._street_models.get(STREET_TILE)
        if ld is None:
            self._draw_streets_flat(span)
            return
        cull = TILE_CULL * TILE_CULL
        scale = pr.Vector3(STREET_XZ, STREET_Y, STREET_XZ)
        for a in range(1, AVENUES):
            x = (a - CENTER + 0.5) * BLOCK
            if (x - cx) ** 2 > cull:
                continue
            for s in range(1, STREETS):
                z = (s - CENTER + 0.5) * BLOCK
                if (x - cx) ** 2 + (z - cz) ** 2 > cull:
                    continue
                # Adjacent tiles overlap (over-scaled); alternate height so the
                # coincident sidewalks/road don't z-fight — one always wins. Each tile
                # sits at its block-centre ground height (gentle terraces over the roll).
                y = STREET_Y_OFF + STREET_PARITY * ((a + s) % 2) + self.ground_y(x, z)
                pr.draw_model_ex(ld.model, pr.Vector3(x, y, z),
                                 pr.Vector3(0, 1, 0), 0.0, scale, pr.WHITE)

    def _draw_streets_flat(self, span: float) -> None:
        """Fallback: the original drawn asphalt grid (if the tile GLB is missing)."""
        for a in range(1, AVENUES):
            x = (a - CENTER + 0.5) * BLOCK
            pr.draw_cube(pr.Vector3(x, 0.02, 0), ROAD_W, 0.04, span, ASPHALT)
            pr.draw_cube(pr.Vector3(x, 0.03, 0), 0.18, 0.04, span, LANE)
        for s in range(1, STREETS):
            z = (s - CENTER + 0.5) * BLOCK
            pr.draw_cube(pr.Vector3(0, 0.02, z), span, 0.04, ROAD_W, ASPHALT)
            pr.draw_cube(pr.Vector3(0, 0.03, z), span, 0.04, 0.18, LANE)

    def _draw_shell(self, ld, x: float, z: float, yaw: float, tint,
                    mul: float = 1.0, vboost: float = HEIGHT_BOOST) -> None:
        """Draw a building model scaled to fit and stretched taller by `vboost`."""
        sx = ld.scale * mul
        # Sink the base just below the road surface so the building's flat bottom
        # isn't coplanar with the tiles (that coincidence z-fights), then ride the
        # terrain so buildings sit on the gentle roll with everything else.
        y = ld.y_off * mul * vboost - BUILDING_SINK + self.ground_y(x, z)
        pr.draw_model_ex(ld.model, pr.Vector3(x, y, z),
                         pr.Vector3(0, 1, 0), yaw, pr.Vector3(sx, sx * vboost, sx), tint)

    def _draw_city(self, cx: float, cz: float) -> None:
        cull = CULL_DIST * CULL_DIST
        for model, x, z, yaw, mul, vboost in self._city:
            if (x - cx) ** 2 + (z - cz) ** 2 > cull:
                continue
            ld = self._models.get(model)
            if ld is None:
                continue
            self._draw_shell(ld, x, z, yaw, pr.WHITE, mul, vboost)

    def _draw_npc(self, n: NpcBuilding) -> None:
        ld = self._models.get(n.model)
        if ld is None:
            return
        self._draw_shell(ld, n.x, n.z, 0.0, pr.WHITE, vboost=self.height_mult(n.x, n.z))
        front = n.z + ld.half_d
        aw = _c(n.awning)
        gy = self.ground_y(n.x, n.z)
        # striped door canopy
        pr.draw_cube(pr.Vector3(n.x, gy + 1.7, front + 0.25), TARGET_W * 0.72, 0.16, 0.7, aw)
        pr.draw_cube(pr.Vector3(n.x, gy + 1.5, front + 0.58), TARGET_W * 0.72, 0.32, 0.06, aw)
        # themed word-sign over the canopy (if any)
        if n.sign:
            sld = self._models.get(n.sign)
            if sld is not None:
                s = sld.scale * (SIGN_W / TARGET_W)
                pr.draw_model_ex(sld.model, pr.Vector3(n.x, gy + 2.2, front + 0.2),
                                 pr.Vector3(0, 1, 0), 0.0, pr.Vector3(s, s, s), pr.WHITE)

    def _draw_parks(self, cx: float, cz: float, season: str) -> None:
        """Each city park: a square lawn (recoloured by season), a crushed-stone path
        cross, and a small central fountain. Trees are planted via the shared tree
        list (see _build_city), so they're drawn — and culled — by _draw_trees."""
        cull = CULL_DIST * CULL_DIST
        grass = _c(PARK_GRASS.get(season, PARK_GRASS["Summer"]))
        path = _c(PARK_PATH)
        kerb = _c(PARK_KERB)
        water = _c(PARK_WATER.get(season, PARK_WATER["Summer"]))
        h = PARK_HALF
        for p in self.parks:
            if (p.x - cx) ** 2 + (p.z - cz) ** 2 > cull:
                continue
            gy = self.ground_y(p.x, p.z)
            # lawn, then a path cross laid just above it (avoid z-fighting with +y)
            pr.draw_plane(pr.Vector3(p.x, gy + 0.04, p.z), pr.Vector2(h * 2, h * 2), grass)
            pr.draw_plane(pr.Vector3(p.x, gy + 0.06, p.z), pr.Vector2(h * 2, 1.6), path)
            pr.draw_plane(pr.Vector3(p.x, gy + 0.06, p.z), pr.Vector2(1.6, h * 2), path)
            # central fountain: stone basin, a thin water disc, a low spout pillar
            pr.draw_cylinder(pr.Vector3(p.x, gy + 0.0, p.z), 1.15, 1.15, 0.45, 18, kerb)
            pr.draw_cylinder(pr.Vector3(p.x, gy + 0.30, p.z), 0.95, 0.95, 0.22, 18, water)
            pr.draw_cylinder(pr.Vector3(p.x, gy + 0.45, p.z), 0.16, 0.16, 0.75, 10, kerb)

    def _draw_trees(self, cx: float, cz: float, season: str) -> None:
        cull = CULL_DIST * CULL_DIST
        suffix = SEASON_SUFFIX.get(season, "")          # ""/_Autumn/_Snow
        tint = _c(SEASON_TINT[season]) if season in SEASON_TINT else pr.WHITE
        for x, z, fam, var, yaw, scl in self._trees:
            if (x - cx) ** 2 + (z - cz) ** 2 > cull:
                continue
            ld = self._tree_models.get(f"{fam}{suffix}_{var}.glb")
            if ld is None:                              # model missing -> primitive
                self._draw_tree_fallback(x, z, scl, season)
                continue
            s = ld.scale * scl
            pr.draw_model_ex(ld.model, pr.Vector3(x, ld.y_off * scl + self.ground_y(x, z), z),
                             pr.Vector3(0, 1, 0), yaw, pr.Vector3(s, s, s), tint)

    def _draw_tree_fallback(self, x: float, z: float, scl: float, season: str) -> None:
        """Cylinder-trunk + sphere-canopy stand-in if a tree model can't load,
        tinted to roughly match the season."""
        h = 2.0 * scl
        gy = self.ground_y(x, z)
        pr.draw_cylinder(pr.Vector3(x, gy, z), 0.12, 0.16, h, 7, _c((110, 78, 52)))
        canopy = {"Autumn": (196, 120, 52), "Winter": (224, 228, 236),
                  "Dead": (122, 96, 64)}.get(season, (66, 132, 72))
        pr.draw_sphere(pr.Vector3(x, gy + h + 0.3, z), 0.8 * scl, _c(canopy))
        pr.draw_sphere(pr.Vector3(x + 0.3, gy + h + 0.7, z), 0.55 * scl, _c(canopy))

    def _draw_building(self, b: Building) -> None:
        gy = self.ground_y(b.x, b.z)
        ld = self._models.get(b.model)
        if ld is None:                       # fallback box if a model is missing
            pr.draw_cube(pr.Vector3(b.x, gy + 2, b.z), TARGET_W, 4, TARGET_W, _c(b.color))
            top = 4.2
        else:
            # leased = full colour; available = greyed so it reads as "for lease"
            tint = pr.WHITE if b.leased else _c((125, 130, 138))
            vb = self.height_mult(b.x, b.z)
            self._draw_shell(ld, b.x, b.z, 0.0, tint, vboost=vb)
            top = ld.top * vb

        # floating status banner above the building
        band = _c(b.color) if b.leased else SIGN_LEASE
        pr.draw_cube(pr.Vector3(b.x, gy + top + 0.7, b.z), TARGET_W * 0.7, 0.7, 0.25, band)
        pr.draw_cube_wires(pr.Vector3(b.x, gy + top + 0.7, b.z), TARGET_W * 0.7, 0.7, 0.25, _c((0, 0, 0), 80))

"""A building's interior as a graph of rooms connected by portals.

A building is no longer a single room: it has floors, and each floor has a hub
(the ground LOBBY, or an ELEVATOR LOBBY upstairs) plus optional east/west WINGS.
You move between rooms through portals — walk-up-and-press-E doorways between a
hub and its wings, and an elevator (a floor menu) linking the hubs. Every room is
a FloorPlan (floorplan.py); this module just wires which rooms exist and how they
connect, reading portal *anchor* zones (entrance/elevator/wing_east/wing_west on
hubs, the return 'door' on wings) out of each room's plan.

Backward compatible: a building with no `structure` is modeled as one wing room,
entered straight from the park — exactly today's behaviour.

No raylib dependency, so the graph is headlessly testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Portal kinds
EXIT = "exit"          # lobby -> back out to the park
ELEVATOR = "elevator"  # hub -> floor menu (any other hub)
DOORWAY = "doorway"    # hub <-> wing on the same floor

PARK = "__park__"      # sentinel destination meaning "leave to the office park"


@dataclass
class Portal:
    kind: str
    pos: tuple                 # (x, z) in THIS room where the CEO interacts
    to: str | None             # destination room key, PARK, or None (elevator menu)
    entry: tuple | None = None  # (x, z) spawn in the destination room
    label: str = ""


# A spread of wing layouts so a building's wings don't all look identical. The
# building's own `plan` is tried first, then the rest cycle in.
WING_POOL = ["hq", "studio", "tower"]


def _room_seed(key: str, base: int) -> int:
    """Deterministic per-room furniture seed (stable across runs, varies per key),
    so two same-template wings still scatter their decor differently."""
    h = 0
    for ch in key:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return (base ^ h) & 0xFFFFFFFF


@dataclass
class RoomInstance:
    key: str
    plan_id: str
    kind: str                  # lobby | elevator_lobby | wing
    level: int
    slot: str                  # hub | east | west | single
    portals: list = field(default_factory=list)
    reception: bool = False    # lobby only: a reception desk/agent spot
    label: str = ""
    seed: int = 0              # per-room furniture seed (set in build_interior)

    def plan(self, plans):
        return plans[self.plan_id]


@dataclass
class BuildingInterior:
    building_id: str
    rooms: dict                # key -> RoomInstance
    entry_room: str            # where the park door drops you (the ground lobby)
    hub_keys: list             # hub room keys, ordered by floor level

    def wings(self) -> list:
        """Wing room keys (the rooms that actually hold desks), in build order."""
        return [k for k, r in self.rooms.items() if r.kind == "wing"]

    def primary_wing(self) -> str:
        """The default workspace room — first wing, else the entry room."""
        ws = self.wings()
        return ws[0] if ws else self.entry_room

    def ceo_office(self) -> str:
        """The room that holds the CEO Desk: the wing on the HIGHEST floor (prefer
        the east wing), so the power desk lives up top. Falls back to the first
        wing, then the entry room, so every building always has one."""
        ws = self.wings()
        if not ws:
            return self.entry_room
        top = max(self.rooms[k].level for k in ws)
        top_wings = [k for k in ws if self.rooms[k].level == top]
        east = [k for k in top_wings if self.rooms[k].slot == "east"]
        return (east or top_wings)[0]

    def floor_menu(self, plans) -> list:
        """[(level, label, room_key, entry_pos)] for the elevator's floor picker."""
        out = []
        for k in self.hub_keys:
            r = self.rooms[k]
            ep = r.plan(plans).point("elevator")
            out.append((r.level, r.label or f"Floor {r.level}", k, ep))
        return out


def _stories(model: str) -> int:
    """Story count from a building model filename (e.g. '4Story_Center.glb' -> 4)."""
    m = re.search(r"(\d+)Story", model or "")
    return int(m.group(1)) if m else 2


def default_structure(model: str, wing_plan: str, reception: bool = True) -> dict:
    """Derive a floors/wings structure from a building's story count so EVERY
    building has an interior even without an authored `structure`:
      - ground floor = lobby (with a reception spot)
      - 1-story building: lobby + one east wing, no elevator
      - N-story building: lobby + (N-1) elevator-lobby floors, each with east+west
        wings. Wing layouts CYCLE through a pool (building's own plan first) so the
        floors don't all look the same.
    """
    n = max(1, _stories(model))
    pool = [wing_plan] + [p for p in WING_POOL if p != wing_plan]
    nxt = [0]

    def next_wing() -> str:
        p = pool[nxt[0] % len(pool)]
        nxt[0] += 1
        return p

    ground = {"hub": "lobby", "reception": reception}
    if n == 1:
        ground["wings"] = {"east": next_wing()}    # a workspace, no elevator
        return {"floors": [ground]}
    floors = [ground]
    for _ in range(1, n):
        floors.append({"hub": "elev_lobby",
                       "wings": {"east": next_wing(), "west": next_wing()}})
    return {"floors": floors}


def for_building(building, plans) -> "BuildingInterior":
    """Build the interior for a Park building, using its authored `structure` or a
    default derived from its model's story count + wing plan.

    Homes are the exception: they're a single room — no floors, no wings, no
    elevator — regardless of how many storeys their facade model has, so they always
    take the legacy single-room path (just the plan, entered straight from the city)."""
    if getattr(building, "home", False):
        return build_interior(building.id, None, building.plan, plans)
    struct = getattr(building, "structure", None) or default_structure(
        building.model, building.plan)
    return build_interior(building.id, struct, building.plan, plans)


def _wing_door(plan):
    """A wing's return-to-hub anchor (its 'door' zone, else front-center)."""
    p = plan.point("door")
    return p if p is not None else plan.grid_to_world(plan.cols / 2.0, plan.rows - 1.5)


def build_interior(building_id: str, structure: dict | None,
                   plan_id: str, plans: dict) -> BuildingInterior:
    """Build the room/portal graph for a building.

    `structure` (optional) = {"floors": [ {"hub": <plan id>, "reception"?: bool,
    "wings"?: {"east"/"west": <plan id>}}, ... ]}. With no structure, the building
    is a single wing room (`plan_id`) entered from the park."""
    bid = building_id
    rooms: dict = {}
    hub_keys: list = []

    # --- no structure: one room, straight off the park (legacy behaviour) ----
    if not structure or not structure.get("floors"):
        key = f"{bid}/single"
        plan = plans[plan_id]
        entrance = plan.point("door") or plan.grid_to_world(plan.cols / 2.0, plan.rows - 1.5)
        rooms[key] = RoomInstance(
            key=key, plan_id=plan_id, kind="wing", level=0, slot="single",
            portals=[Portal(EXIT, entrance, PARK, label="Leave")], label="",
            seed=_room_seed(key, plan.furniture_seed),
        )
        return BuildingInterior(bid, rooms, key, [key])

    # --- structured building: floors of hub + wings -------------------------
    floors = structure["floors"]
    for level, fl in enumerate(floors):
        hub_id = fl["hub"]
        hub_key = f"{bid}/L{level}/hub"
        hub_plan = plans[hub_id]
        rooms[hub_key] = RoomInstance(
            key=hub_key, plan_id=hub_id, kind=hub_plan.kind, level=level, slot="hub",
            reception=bool(fl.get("reception")),
            label="Lobby" if level == 0 else f"Floor {level}",
            seed=_room_seed(hub_key, hub_plan.furniture_seed),
        )
        hub_keys.append(hub_key)
        for side, wing_id in fl.get("wings", {}).items():
            wkey = f"{bid}/L{level}/{side}"
            rooms[wkey] = RoomInstance(
                key=wkey, plan_id=wing_id, kind="wing", level=level, slot=side,
                label=f"Floor {level} {side.title()} Wing",
                seed=_room_seed(wkey, plans[wing_id].furniture_seed),
            )

    multi_floor = len(floors) > 1
    for level, fl in enumerate(floors):
        hub_key = f"{bid}/L{level}/hub"
        hub = rooms[hub_key]
        hub_plan = hub.plan(plans)

        # ground lobby: a way back out to the park
        if level == 0:
            ent = hub_plan.point("entrance") or hub_plan.point("elevator")
            hub.portals.append(Portal(EXIT, ent, PARK, label="Exit to park"))

        # elevator: present on every hub when the building has more than one floor
        if multi_floor:
            epos = hub_plan.point("elevator")
            if epos is not None:
                hub.portals.append(Portal(ELEVATOR, epos, None, label="Elevator"))

        # doorways: hub <-> each wing on this floor (bidirectional)
        for side in ("west", "east"):
            wkey = f"{bid}/L{level}/{side}"
            if wkey not in rooms:
                continue
            anchor = hub_plan.point(f"wing_{side}")
            wing_plan = rooms[wkey].plan(plans)
            wdoor = _wing_door(wing_plan)
            if anchor is not None:
                hub.portals.append(Portal(DOORWAY, anchor, wkey, entry=wdoor,
                                          label=rooms[wkey].label))
                rooms[wkey].portals.append(Portal(DOORWAY, wdoor, hub_key, entry=anchor,
                                                  label=hub.label))

    return BuildingInterior(bid, rooms, f"{bid}/L0/hub", hub_keys)

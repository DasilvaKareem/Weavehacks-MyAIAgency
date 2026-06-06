"""Floor plans: the data describing one office interior.

Each leased building has a floor plan — its room size, where the desks sit, and a
set of named *zones* (meeting rooms, lounges, coffee, whiteboard, door). The rest
of the game (Scene rendering, the navgrid, zone lookups, bot homes/seats, the
meeting gather) reads the *active* plan instead of hardcoded constants, so
entering a different building swaps the whole interior.

Plans are authored as JSON templates (assets/floor_plans.json) and attached to
buildings by id; a built-in DEFAULT_HQ keeps everything working headlessly and as
a fallback. A zone's `kind` drives behaviour:
    meeting    -> a conference table + ring of stools (bots gather/sit here)
    lounge     -> a couch (bots sit to relax)
    coffee / whiteboard / door -> plain walk-to landmarks for roaming
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field

from . import config

PLANS_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "assets", "floor_plans.json")

# Seats around a meeting table (shared by the scene's stool drawing and the
# game's bot-gathering); a ring of this many stools at this radius.
MEETING_SEAT_COUNT = 6
MEETING_SEAT_R = 1.35


@dataclass
class Zone:
    name: str
    kind: str          # meeting | lounge | coffee | whiteboard | door
    col: float
    row: float


@dataclass
class FloorPlan:
    id: str
    cols: int
    rows: int
    desk_cols: list
    desk_rows: list
    zones: list                       # list[Zone]
    furniture_seed: int = config.FURNITURE_SEED
    name: str = ""
    kind: str = "wing"                # wing (desks) | lobby | elevator_lobby

    # -- coordinates (centered on this plan's own size) ----------------------
    def grid_to_world(self, col: float, row: float) -> tuple[float, float]:
        x = (col - (self.cols - 1) / 2.0) * config.TILE
        z = (row - (self.rows - 1) / 2.0) * config.TILE
        return x, z

    def bounds(self) -> tuple[float, float]:
        """Playable half-extents (margin from the walls), like the old office."""
        return (self.cols * config.TILE / 2.0 - 0.8,
                self.rows * config.TILE / 2.0 - 0.8)

    # -- zones ---------------------------------------------------------------
    def zone(self, name: str) -> Zone | None:
        for z in self.zones:
            if z.name == name:
                return z
        return None

    def point(self, name: str) -> tuple[float, float] | None:
        z = self.zone(name)
        return self.grid_to_world(z.col, z.row) if z else None

    def zone_names(self) -> list:
        return [z.name for z in self.zones]

    def zones_of(self, kind: str) -> list:
        return [z for z in self.zones if z.kind == kind]

    def zone_points(self, kind: str) -> list:
        return [self.grid_to_world(z.col, z.row) for z in self.zones_of(kind)]

    # -- meeting tables ------------------------------------------------------
    def meeting_points(self) -> list:
        return self.zone_points("meeting")

    def primary_meeting(self) -> tuple[float, float]:
        pts = self.meeting_points()
        return pts[0] if pts else self.grid_to_world(self.cols / 2.0, self.rows / 2.0)

    def meeting_seats(self, center: tuple[float, float] | None = None,
                      n: int = MEETING_SEAT_COUNT, r: float = MEETING_SEAT_R) -> list:
        cx, cz = center if center is not None else self.primary_meeting()
        return [(cx + math.cos(2 * math.pi * i / n) * r,
                 cz + math.sin(2 * math.pi * i / n) * r) for i in range(n)]

    # -- desks ---------------------------------------------------------------
    def desk_capacity(self) -> int:
        return len(self.desk_cols) * len(self.desk_rows)

    def desk_slot(self, index: int) -> tuple[int, int]:
        """Grid (col, row) for the Nth desk, row-major across the desk grid."""
        ncols = max(1, len(self.desk_cols))
        row = self.desk_rows[(index // ncols) % max(1, len(self.desk_rows))]
        col = self.desk_cols[index % ncols]
        return col, row


def _plan_from_dict(pid: str, d: dict) -> FloorPlan:
    zones = [Zone(name=z["name"], kind=z["kind"], col=z["col"], row=z["row"])
             for z in d.get("zones", [])]
    return FloorPlan(
        id=pid, name=d.get("name", pid),
        cols=d["cols"], rows=d["rows"],
        desk_cols=list(d["desk_cols"]), desk_rows=list(d["desk_rows"]),
        zones=zones, furniture_seed=d.get("furniture_seed", config.FURNITURE_SEED),
        kind=d.get("kind", "wing"),
    )


def load_plans(path: str = PLANS_PATH) -> dict:
    """Load all authored floor-plan templates, keyed by id. Falls back to just the
    built-in HQ if the file is missing or unreadable."""
    plans = {"hq": DEFAULT_HQ}
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        for pid, d in doc.get("plans", {}).items():
            plans[pid] = _plan_from_dict(pid, d)
    except (OSError, ValueError, KeyError):
        pass
    return plans


# Built-in HQ — mirrors the original hardcoded office (16x11, three desk rows,
# zones in the open front band) so behaviour is unchanged until a building swaps
# the plan. Also the safe fallback when JSON is missing.
DEFAULT_HQ = FloorPlan(
    id="hq", name="HQ",
    cols=16, rows=13,                          # deep enough for desks + a meeting ring + lounge
    desk_cols=[3, 5, 7, 9, 11, 13], desk_rows=[2, 4, 6],
    furniture_seed=config.FURNITURE_SEED,
    zones=[
        Zone("whiteboard", "whiteboard", 2.0, 9.0),
        Zone("coffee", "coffee", 13.0, 9.0),
        Zone("door", "door", 11.0, 11.0),
        Zone("meeting", "meeting", 7.5, 9.0),
        Zone("lounge", "lounge", 4.0, 11.0),
    ],
)

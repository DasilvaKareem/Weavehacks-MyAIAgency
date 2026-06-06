"""Named landmarks the bots navigate between — a facade over the *active* floor plan.

Zones used to be hardcoded constants for a single office. Now each building has
its own FloorPlan (see floorplan.py), and this module just forwards to whichever
plan is currently active. Callers keep using `zones.point(name)`,
`zones.all_names()`, `zones.meeting_center()`, etc. unchanged; switching buildings
is a single `zones.set_active(plan)` call.

A zone is still just a named spot a bot can walk to (coffee, whiteboard, meeting,
lounge, door); the active plan owns the actual positions and which zones exist.
"""
from __future__ import annotations

from . import floorplan

# The plan whose zones are currently "live". Defaults to the built-in HQ so the
# module is usable headlessly and before any building is entered.
_active: floorplan.FloorPlan = floorplan.DEFAULT_HQ


def set_active(plan: floorplan.FloorPlan) -> None:
    global _active
    _active = plan


def active() -> floorplan.FloorPlan:
    return _active


# -- zone lookups (delegate to the active plan) ------------------------------
def all_names() -> list:
    return _active.zone_names()


def point(name: str) -> tuple[float, float] | None:
    return _active.point(name)


def points(names) -> list:
    out = []
    for n in names:
        p = point(n)
        if p is not None:
            out.append(p)
    return out


# -- meeting table -----------------------------------------------------------
def meeting_center() -> tuple[float, float]:
    return _active.primary_meeting()


def meeting_centers() -> list:
    """World points of every meeting table in the active plan."""
    return _active.meeting_points()


def meeting_seats(center: tuple[float, float] | None = None) -> list:
    return _active.meeting_seats(center)


# -- lounges -----------------------------------------------------------------
def lounge_points() -> list:
    return _active.zone_points("lounge")

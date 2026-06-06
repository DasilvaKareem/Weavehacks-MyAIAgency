"""Seats the CEO can sit at — meeting stools and lounge couches in the active plan.

A "seat" is a world spot plus the yaw a seated character should face. When the CEO
presses C we pick the nearest free seat (see Player.update); if none is within
reach the CEO just sits in place. Bots manage their own desk chairs in behavior.py,
so those aren't offered here.
"""
from __future__ import annotations

import math

from . import zones

# How close the CEO must be to a seat to snap onto it (world units), and how close
# another character has to be for that seat to count as taken.
SIT_RANGE = 1.6
OCCUPIED_EPS = 0.5


def seats() -> list[tuple[float, float, float]]:
    """Every public seat in the active plan as (x, z, yaw_degrees).

    Meeting stools face their table; lounge couch spots face +z (the couch's
    front, matching how scene.py draws the lounge)."""
    out: list[tuple[float, float, float]] = []
    for cx, cz in zones.meeting_centers():
        for sx, sz in zones.meeting_seats((cx, cz)):
            yaw = math.degrees(math.atan2(cx - sx, cz - sz))   # turn toward the table
            out.append((sx, sz, yaw))
    for lx, lz in zones.lounge_points():
        out.append((lx, lz + 0.1, 0.0))                        # couch faces +z
    return out


def _is_free(sx: float, sz: float, occupied) -> bool:
    for ox, oz in occupied or ():
        if (sx - ox) ** 2 + (sz - oz) ** 2 <= OCCUPIED_EPS * OCCUPIED_EPS:
            return False
    return True


def nearest_seat(x: float, z: float, occupied=None, max_dist: float = SIT_RANGE):
    """The closest free seat to (x, z) within max_dist, else None.

    Returns (x, z, yaw) so the caller can snap the character onto it and face the
    right way. `occupied` is an iterable of (x, z) points already taken (other
    characters), which are skipped."""
    best, best_d2 = None, max_dist * max_dist
    for sx, sz, yaw in seats():
        d2 = (sx - x) ** 2 + (sz - z) ** 2
        if d2 <= best_d2 and _is_free(sx, sz, occupied):
            best, best_d2 = (sx, sz, yaw), d2
    return best

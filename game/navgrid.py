"""Walkability grid + A* pathfinding over the office floor.

The office has no runtime collision (movement is otherwise free), so to make bots
route *around* desks and furniture we bake a static occupancy grid: the floor is
diced into half-tile cells and every cell covered by a desk or a solid prop (plus
a one-cell margin) is marked blocked. A* over the free cells yields a list of
world-space waypoints the locomotion layer steers through.

The grid is cheap to hold (the office is ~32x22 cells) and only rebuilt when the
furniture actually changes — on hire (a new desk) or a shop purchase — never per
frame. Rugs are flat floor coverings, so they're walkable and don't block.
"""
from __future__ import annotations

import heapq
import math

from . import config

CELL = 0.5  # world units per grid cell (half a tile) — smooths paths vs full tiles

# Half-extents (world units) of each prop kind's blocking footprint. Rugs AND
# couches are omitted on purpose: rugs lie flat and couches are sat *on* (a bot
# walks up and sits), so both are walkable.
_FOOTPRINT: dict[str, tuple[float, float]] = {
    "chair":   (0.30, 0.30),
    "plant":   (0.40, 0.40),
    "cabinet": (0.45, 0.35),
    "cooler":  (0.25, 0.25),
    "bin":     (0.20, 0.20),
}

# Desk top is ~1.4 (x) by 0.8 (z); block that plus a little for the tucked chair.
_DESK_HALF = (0.70, 0.45)

# Default clearance added around every footprint. Kept below a full cell so the
# desk pod doesn't bloat into the open floor, while still keeping bots off corners.
_MARGIN = 0.25


def _prop_footprint(prop) -> tuple[float, float] | None:
    """(half_x, half_z) blocking box for a furniture Prop, or None if walkable."""
    return _FOOTPRINT.get(prop.kind)


class NavGrid:
    def __init__(self, world_w: float, world_d: float) -> None:
        self.w = world_w
        self.d = world_d
        self.cols = max(1, int(math.ceil(world_w / CELL)))
        self.rows = max(1, int(math.ceil(world_d / CELL)))
        # blocked[j][i] — True means impassable.
        self._blocked = [[False] * self.cols for _ in range(self.rows)]
        # Keep bots inside the same playable bounds the CEO uses (margin from
        # walls); cells whose center is outside are blocked.
        self._bx = world_w / 2.0 - 0.8
        self._bz = world_d / 2.0 - 0.8
        self._block_border()

    # -- coordinate helpers --------------------------------------------------
    def _to_cell(self, x: float, z: float) -> tuple[int, int]:
        i = int((x + self.w / 2.0) / CELL)
        j = int((z + self.d / 2.0) / CELL)
        return max(0, min(self.cols - 1, i)), max(0, min(self.rows - 1, j))

    def _to_world(self, i: int, j: int) -> tuple[float, float]:
        x = -self.w / 2.0 + (i + 0.5) * CELL
        z = -self.d / 2.0 + (j + 0.5) * CELL
        return x, z

    def _in_bounds(self, i: int, j: int) -> bool:
        return 0 <= i < self.cols and 0 <= j < self.rows

    def free(self, i: int, j: int) -> bool:
        return self._in_bounds(i, j) and not self._blocked[j][i]

    # -- construction --------------------------------------------------------
    def _block_border(self) -> None:
        for j in range(self.rows):
            for i in range(self.cols):
                x, z = self._to_world(i, j)
                if abs(x) > self._bx or abs(z) > self._bz:
                    self._blocked[j][i] = True

    def block_aabb(self, cx: float, cz: float, half_x: float, half_z: float,
                   margin: float = _MARGIN) -> None:
        """Mark every cell overlapping the axis-aligned box (plus margin)."""
        hx, hz = half_x + margin, half_z + margin
        i0, j0 = self._to_cell(cx - hx, cz - hz)
        i1, j1 = self._to_cell(cx + hx, cz + hz)
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                if self._in_bounds(i, j):
                    self._blocked[j][i] = True

    def add_desk(self, x: float, z: float) -> None:
        self.block_aabb(x, z, _DESK_HALF[0], _DESK_HALF[1])

    def add_prop(self, prop) -> None:
        fp = _prop_footprint(prop)
        if fp is not None:
            self.block_aabb(prop.x, prop.z, fp[0], fp[1])

    # -- queries -------------------------------------------------------------
    def is_blocked(self, x: float, z: float) -> bool:
        i, j = self._to_cell(x, z)
        return self._blocked[j][i]

    def snap_point(self, x: float, z: float) -> tuple[float, float]:
        """World center of the nearest free cell to (x, z) — a reachable target."""
        return self._to_world(*self.nearest_free(x, z))

    def nearest_free(self, x: float, z: float) -> tuple[int, int]:
        """Cell at (x,z) if free, else the closest free cell by ring search."""
        i, j = self._to_cell(x, z)
        if self.free(i, j):
            return i, j
        for r in range(1, max(self.cols, self.rows) + 1):
            best = None
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    if max(abs(di), abs(dj)) != r:  # ring perimeter only
                        continue
                    ni, nj = i + di, j + dj
                    if self.free(ni, nj):
                        d = di * di + dj * dj
                        if best is None or d < best[0]:
                            best = (d, ni, nj)
            if best is not None:
                return best[1], best[2]
        return i, j  # whole grid blocked — give up gracefully

    # -- A* ------------------------------------------------------------------
    def find_path(self, start, goal) -> list[tuple[float, float]]:
        """World-space waypoints from start to goal, [] if unreachable.

        start/goal are (x, z). Both are snapped to the nearest free cell, so a
        goal under a desk routes to the open floor beside it. The returned list
        ends at the snapped goal cell center; callers may append the exact goal.
        """
        si, sj = self.nearest_free(*start)
        gi, gj = self.nearest_free(*goal)
        if (si, sj) == (gi, gj):
            return [self._to_world(gi, gj)]

        # 8-connected; diagonals cost sqrt(2) and may not cut blocked corners.
        neigh = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
                 (-1, -1, 1.41421356), (1, -1, 1.41421356),
                 (-1, 1, 1.41421356), (1, 1, 1.41421356)]

        def h(i, j):
            dx, dy = abs(i - gi), abs(j - gj)
            return (dx + dy) + (1.41421356 - 2) * min(dx, dy)  # octile

        open_heap = [(h(si, sj), 0.0, si, sj)]
        came: dict[tuple[int, int], tuple[int, int]] = {}
        g_cost = {(si, sj): 0.0}
        seen: set[tuple[int, int]] = set()

        while open_heap:
            _, g, i, j = heapq.heappop(open_heap)
            if (i, j) in seen:
                continue
            seen.add((i, j))
            if (i, j) == (gi, gj):
                return self._reconstruct(came, (gi, gj))
            for di, dj, cost in neigh:
                ni, nj = i + di, j + dj
                if not self.free(ni, nj):
                    continue
                if di != 0 and dj != 0:  # no corner-cutting through blocked cells
                    if not (self.free(i + di, j) and self.free(i, j + dj)):
                        continue
                ng = g + cost
                if ng < g_cost.get((ni, nj), float("inf")):
                    g_cost[(ni, nj)] = ng
                    came[(ni, nj)] = (i, j)
                    heapq.heappush(open_heap, (ng + h(ni, nj), ng, ni, nj))
        return []  # no path

    def _reconstruct(self, came, end) -> list[tuple[float, float]]:
        cells = [end]
        while end in came:
            end = came[end]
            cells.append(end)
        cells.reverse()
        return self._simplify(cells)

    def _simplify(self, cells) -> list[tuple[float, float]]:
        """Drop intermediate cells that continue in the same direction, so the
        path is a handful of corner waypoints instead of one per cell."""
        if len(cells) <= 2:
            return [self._to_world(i, j) for i, j in cells]
        out = [cells[0]]
        for k in range(1, len(cells) - 1):
            pi, pj = cells[k - 1]
            ci, cj = cells[k]
            ni, nj = cells[k + 1]
            if (ci - pi, cj - pj) != (ni - ci, nj - cj):  # direction changed
                out.append(cells[k])
        out.append(cells[-1])
        return [self._to_world(i, j) for i, j in out]


def build(desks, props, cols: int | None = None, rows: int | None = None) -> NavGrid:
    """Assemble a NavGrid from desk centers [(x,z), ...] and furniture Props.

    `cols`/`rows` size the grid to the active floor plan (defaults to the legacy
    office size when omitted)."""
    c = cols if cols is not None else config.GRID_COLS
    r = rows if rows is not None else config.GRID_ROWS
    grid = NavGrid(c * config.TILE, r * config.TILE)
    for (x, z) in desks:
        grid.add_desk(x, z)
    for prop in props:
        grid.add_prop(prop)
    return grid

"""Shared character locomotion: facing, animation-state, and path following.

Both the CEO (driven by player input) and the hired bots (driven by a navgrid
path) need the same low-level motion behaviour — turn smoothly toward where
you're going, and pick Idle/Walk/Run/Jump from how you're moving. That logic
used to live only in Player; it's factored out here so bots reuse it verbatim,
and a PathFollower adds the "walk this list of waypoints" driver on top.

No raylib dependency, so it stays headlessly testable.
"""
from __future__ import annotations

import math

from . import config

# Speeds (world units / sec). The CEO keeps the brisker WALK/RUN; bots amble.
WALK_SPEED = 4.0
RUN_SPEED = 8.0
BOT_SPEED = 2.6
TURN_LERP = 14.0           # how fast a character rotates to face its motion
ARRIVE_EPS = 0.14          # within this distance, a waypoint counts as reached

# Keep characters inside the office (half-extents, with a margin from the walls).
# Single source of truth shared by the player and the bots.
BOUND_X = config.GRID_COLS * config.TILE / 2.0 - 0.8
BOUND_Z = config.GRID_ROWS * config.TILE / 2.0 - 0.8

# Active half-extents, switchable at runtime (the office park is much larger than
# the office). Defaults to the office; main.py swaps these on mode change. Bots
# only move in the office, so changing this during park mode is safe.
_BOUNDS = [BOUND_X, BOUND_Z]


def set_bounds(bx: float, bz: float) -> None:
    _BOUNDS[0], _BOUNDS[1] = bx, bz


def reset_bounds() -> None:
    _BOUNDS[0], _BOUNDS[1] = BOUND_X, BOUND_Z


def shortest_angle(a: float, b: float) -> float:
    """Smallest signed degrees to rotate from a to b."""
    return (b - a + 180.0) % 360.0 - 180.0


def clamp_to_bounds(x: float, z: float) -> tuple[float, float]:
    bx, bz = _BOUNDS
    return max(-bx, min(bx, x)), max(-bz, min(bz, z))


def face_dir(ch, dx: float, dz: float, dt: float) -> None:
    """Smoothly turn `ch` to face travel direction (dx, dz). No-op if ~zero."""
    if abs(dx) < 1e-6 and abs(dz) < 1e-6:
        return
    target_yaw = math.degrees(math.atan2(dx, dz))
    ch.yaw += shortest_angle(ch.yaw, target_yaw) * min(1.0, TURN_LERP * dt)


# Clip that plays stand->sit then holds, used when a character is seated.
SIT_ANIM = "SitDown"


def apply_anim(ch, *, moving: bool, running: bool = False,
               grounded: bool = True, seated: bool = False) -> None:
    """Set the character's animation clip from its motion state, restarting the
    clip cleanly on a state change (same rule Player used).

    `seated` overrides everything else: it plays the SitDown clip once and holds
    the final (seated) pose by clearing anim_loop. Any other state loops."""
    if seated:
        new_anim, loop = SIT_ANIM, False
    elif not grounded:
        new_anim, loop = "Jump", True
    elif moving:
        new_anim, loop = ("Run" if running else "Walk"), True
    else:
        new_anim, loop = config.ANIM_IDLE_NAME, True
    ch.anim_loop = loop
    if new_anim != ch.anim_name:
        ch.anim_name = new_anim
        ch._frame = 0.0


def move_toward(ch, tx: float, tz: float, speed: float, dt: float) -> bool:
    """Step `ch` toward world target (tx, tz); face it. Return True on arrival.

    Doesn't overshoot: if the remaining distance is within this frame's step,
    snaps to the target. Stays inside the office bounds. Leaves animation to the
    caller (so a follower can choose Walk vs Idle for the whole path)."""
    dx, dz = tx - ch.x, tz - ch.z
    dist = math.hypot(dx, dz)
    if dist <= ARRIVE_EPS:
        return True
    face_dir(ch, dx, dz, dt)
    step = speed * dt
    if step >= dist:
        ch.x, ch.z = clamp_to_bounds(tx, tz)
        return True
    ch.x, ch.z = clamp_to_bounds(ch.x + dx / dist * step, ch.z + dz / dist * step)
    return False


class PathFollower:
    """Walks a character along a list of world-space waypoints.

    Set a path with set_path(); call update() each frame. It steers toward the
    current waypoint, advances when reached, and reports arrival at the end.
    Animation (Walk while moving, Idle when stopped) is applied here so a bot
    just needs to feed it a path.
    """

    def __init__(self, speed: float = BOT_SPEED) -> None:
        self.speed = speed
        self._path: list[tuple[float, float]] = []
        self._i = 0

    @property
    def active(self) -> bool:
        return self._i < len(self._path)

    @property
    def goal(self) -> tuple[float, float] | None:
        return self._path[-1] if self._path else None

    def set_path(self, waypoints) -> None:
        self._path = list(waypoints or [])
        self._i = 0

    def clear(self) -> None:
        self._path = []
        self._i = 0

    def update(self, ch, dt: float) -> bool:
        """Advance along the path. Returns True the moment the end is reached."""
        if not self.active:
            apply_anim(ch, moving=False)
            return False
        tx, tz = self._path[self._i]
        if move_toward(ch, tx, tz, self.speed, dt):
            self._i += 1
        if self.active:
            apply_anim(ch, moving=True)
            return False
        apply_anim(ch, moving=False)   # reached the final waypoint this frame
        return True

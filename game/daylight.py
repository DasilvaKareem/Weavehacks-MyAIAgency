"""Time-of-day lighting: a smooth diurnal cycle through eight named phases.

The world used to have one fixed sky color and a light direction baked into the
character shader. This module drives both from a game clock that walks the day
through eight phases — Midnight, Dawn, Morning, Noon, Afternoon, Dusk, Evening,
Night — interpolating the sky color, key-light direction/color, and ambient fill
between adjacent phases. The result: the office and park warm at sunrise, cool to
neutral at noon, glow orange at dusk, and dim to deep blue overnight.

`DayCycle` is the only thing callers touch: `advance(dt)` each frame, then read
`sky_color()` for the background and `model_tint()` for the character tint (see
ModelRegistry.set_daylight).
"""
from __future__ import annotations

import math
import pyray as pr

from . import config


class _Phase:
    """One keyframe in the day. `sky` is the clear-color (r,g,b); `sun` the world
    direction toward the key light; `tint` its color (multiplied onto characters,
    ~1.0 = white); `key`/`ambient` the directional vs. fill strength."""

    __slots__ = ("name", "sky", "sun", "tint", "key", "ambient")

    def __init__(self, name, sky, sun, tint, key, ambient):
        self.name = name
        self.sky = sky
        self.sun = _norm(sun)
        self.tint = tint
        self.key = key
        self.ambient = ambient


def _norm(v):
    m = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return (v[0] / m, v[1] / m, v[2] / m)


# Eight phases in chronological order; each occupies 1/8 of the cycle and the
# renderer blends smoothly from one to the next. The sun rises in the east
# (-x) at dawn, passes overhead at noon, sets in the west (+x) at dusk.
PHASES = [
    #        name          sky(r,g,b)         sun dir (toward light)  tint(r,g,b)           key   ambient
    _Phase("Midnight",  (10, 14, 30),     (0.10, 0.50, -0.85),  (0.55, 0.62, 0.95),  0.10, 0.16),
    _Phase("Dawn",      (190, 150, 165),  (-0.80, 0.25, 0.50),  (1.05, 0.82, 0.78),  0.35, 0.30),
    _Phase("Morning",   (165, 200, 232),  (-0.50, 0.70, 0.50),  (1.00, 0.97, 0.88),  0.58, 0.42),
    _Phase("Noon",      (196, 214, 235),  (0.05, 1.00, 0.15),   (1.00, 1.00, 1.00),  0.68, 0.50),
    _Phase("Afternoon", (200, 206, 224),  (0.50, 0.72, -0.40),  (1.00, 0.95, 0.84),  0.62, 0.46),
    _Phase("Dusk",      (236, 142, 92),   (0.82, 0.22, -0.45),  (1.10, 0.72, 0.52),  0.40, 0.32),
    _Phase("Evening",   (74, 62, 100),    (0.50, 0.30, -0.70),  (0.78, 0.66, 0.92),  0.20, 0.24),
    _Phase("Night",     (24, 30, 56),     (0.20, 0.55, -0.75),  (0.60, 0.68, 0.95),  0.12, 0.18),
]

PHASE_NAMES = [p.name for p in PHASES]


def _smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def _lerp(a, b, t):
    return a + (b - a) * t


def _lerp3(a, b, t):
    return (_lerp(a[0], b[0], t), _lerp(a[1], b[1], t), _lerp(a[2], b[2], t))


class DayCycle:
    """Tracks the time of day and blends the active lighting between phases.

    The full day takes `day_seconds` of real time. `advance(dt)` moves the clock;
    everything else is a read of the current blended state.
    """

    def __init__(self, day_seconds: float | None = None, start: str = "Morning") -> None:
        self.day_seconds = day_seconds or config.DAY_SECONDS
        self.clock = PHASE_NAMES.index(start) / len(PHASES) * self.day_seconds \
            if start in PHASE_NAMES else 0.0
        self._recompute()

    def advance(self, dt: float) -> int:
        """Move the clock by `dt` real-seconds. Returns how many whole days rolled
        over (the clock passing Midnight) so the calendar can count days."""
        self.clock += dt
        rolled = int(self.clock // self.day_seconds)
        if rolled:
            self.clock %= self.day_seconds
        self._recompute()
        return rolled

    def skip_phase(self) -> int:
        """Jump straight to the start of the next phase (handy for a peek key).
        Returns 1 if that jump crossed Midnight into a new day, else 0."""
        step = self.day_seconds / len(PHASES)
        nxt = (math.floor(self.clock / step) + 1) * step
        rolled = int(nxt // self.day_seconds)
        self.clock = nxt % self.day_seconds
        self._recompute()
        return rolled

    def _recompute(self) -> None:
        n = len(PHASES)
        pos = self.clock / self.day_seconds * n     # [0, n)
        i = int(pos) % n
        j = (i + 1) % n
        t = _smoothstep(pos - math.floor(pos))
        a, b = PHASES[i], PHASES[j]
        self._sky = _lerp3(a.sky, b.sky, t)
        self.light_dir = _norm(_lerp3(a.sun, b.sun, t))
        self.light_color = _lerp3(a.tint, b.tint, t)
        self.key = _lerp(a.key, b.key, t)
        self.ambient = _lerp(a.ambient, b.ambient, t)
        # Name the phase we're closest to, so the HUD reads cleanly.
        self.phase_name = (a if t < 0.5 else b).name

    def sky_color(self) -> pr.Color:
        r, g, b = self._sky
        return pr.Color(int(r), int(g), int(b), 255)

    def model_tint(self) -> pr.Color:
        """A draw tint that bakes the current lighting onto characters: the
        key+fill brightness scales it down overnight, the light color warms it at
        dawn/dusk. ~white at midday. Folded into colDiffuse at draw time."""
        b = min(1.0, self.ambient + self.key * 0.9)
        r, g, bl = self.light_color
        return pr.Color(int(min(1.0, r * b) * 255),
                        int(min(1.0, g * b) * 255),
                        int(min(1.0, bl * b) * 255), 255)

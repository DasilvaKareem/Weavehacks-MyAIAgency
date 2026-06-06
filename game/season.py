"""Slow seasonal cycle: Summer -> Autumn -> Winter -> Dead, swapping tree foliage.

Mirrors daylight.DayCycle but on a much slower clock. The park reads `name`
each frame to choose which Ultimate Nature Pack tree variant to draw: the base
model is summer (green), `_Autumn` recolours the canopy, `_Snow` caps it in snow
for winter. The pack ships no leafless/dead model, so the `Dead` phase reuses the
autumn geometry darkened to a dried, lifeless brown via SEASON_TINT (a multiply
tint on the green summer canopy would only darken it, not brown it — autumn's warm
canopy takes the tint cleanly). Drop real `*_Dead_*.glb` files into assets/nature
and point SEASON_SUFFIX["Dead"] at them to swap in proper art.

`SeasonClock` is the only thing callers touch: `advance(dt)` each frame, read
`name` for the current season, `progress()` for a 0..1 bar.
"""
from __future__ import annotations

SEASONS = ["Summer", "Autumn", "Winter", "Dead"]

# Filename infix the nature pack uses per season (summer is the bare base model).
# Dead has no art of its own, so it borrows the autumn foliage and recolours it.
SEASON_SUFFIX = {"Summer": "", "Autumn": "_Autumn", "Winter": "_Snow", "Dead": "_Autumn"}

# Multiplicative model tint per season; absent means no tint (draw as authored).
# Dead darkens the borrowed autumn canopy to a dried brown so it reads as bare/dead.
SEASON_TINT = {"Dead": (150, 122, 90)}

SEASON_SECONDS = 75.0   # real seconds each season lasts (~5 min per full year)


class SeasonClock:
    """Walks Summer -> Autumn -> Winter -> Summer on a fixed-length clock."""

    def __init__(self, season: str = "Summer") -> None:
        self.i = SEASONS.index(season) if season in SEASONS else 0
        self.t = 0.0

    @property
    def name(self) -> str:
        return SEASONS[self.i]

    def advance(self, dt: float) -> None:
        self.t += dt
        if self.t >= SEASON_SECONDS:
            self.t -= SEASON_SECONDS
            self.i = (self.i + 1) % len(SEASONS)

    def skip(self) -> None:
        """Jump straight to the next season (e.g. for a debug key)."""
        self.t = 0.0
        self.i = (self.i + 1) % len(SEASONS)

    def progress(self) -> float:
        return min(1.0, self.t / SEASON_SECONDS)

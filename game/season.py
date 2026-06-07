"""Slow seasonal cycle: Summer -> Autumn -> Winter -> Dead, swapping tree foliage.

The season is derived from the in-game calendar's day count (a season lasts
DAYS_PER_SEASON days), so the foliage cycle is locked to the date — it can't drift
out of sync with the clock, and it persists for free with the calendar (no
separate timer to save). The park reads `name` each frame to choose which Ultimate
Nature Pack tree variant to draw: the base model is summer (green), `_Autumn`
recolours the canopy, `_Snow` caps it in snow for winter. The pack ships no
leafless/dead model, so the `Dead` phase reuses the autumn geometry darkened to a
dried, lifeless brown via SEASON_TINT (a multiply tint on the green summer canopy
would only darken it, not brown it — autumn's warm canopy takes the tint cleanly).
Drop real `*_Dead_*.glb` files into assets/nature and point SEASON_SUFFIX["Dead"]
at them to swap in proper art.

`SeasonClock` is the only thing callers touch: `set_day(calendar.day)` each frame,
read `name` for the current season, `progress()` for a 0..1 bar.
"""
from __future__ import annotations

SEASONS = ["Summer", "Autumn", "Winter", "Dead"]

# Filename infix the nature pack uses per season (summer is the bare base model).
# Dead has no art of its own, so it borrows the autumn foliage and recolours it.
SEASON_SUFFIX = {"Summer": "", "Autumn": "_Autumn", "Winter": "_Snow", "Dead": "_Autumn"}

# Multiplicative model tint per season; absent means no tint (draw as authored).
# Dead darkens the borrowed autumn canopy to a dried brown so it reads as bare/dead.
SEASON_TINT = {"Dead": (150, 122, 90)}

DAYS_PER_SEASON = 7   # in-game days each season lasts (one in-game week)


class SeasonClock:
    """Derives the current season from the calendar's running day count: a season
    lasts DAYS_PER_SEASON days, cycling Summer -> Autumn -> Winter -> Dead -> ...
    Call `set_day(calendar.day)` each frame to keep it in sync with the date."""

    def __init__(self, day: int = 0) -> None:
        self.day = max(0, int(day))

    def set_day(self, day: int) -> None:
        """Sync to the in-game calendar's running day count (call each frame)."""
        self.day = max(0, int(day))

    @property
    def i(self) -> int:
        return (self.day // DAYS_PER_SEASON) % len(SEASONS)

    @property
    def name(self) -> str:
        return SEASONS[self.i]

    def progress(self) -> float:
        """How far through the current season, 0..1 (by whole in-game days)."""
        return (self.day % DAYS_PER_SEASON) / DAYS_PER_SEASON

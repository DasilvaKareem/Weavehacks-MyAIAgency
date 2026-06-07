"""In-game calendar: turns the day/night cycle's day count into a readable date.

The day/night `DayCycle` loops once per in-game day (Midnight -> Midnight). This
module counts those loops and maps the running total onto a simple calendar — a
7-day week, 30-day months, 12 months a year — so the HUD can show the date and
other systems can key off it. Day index 0 is the first day: Year 1, the 1st, a
Monday.

`GameCalendar` is the only thing callers touch: `advance(rollovers)` with the
count returned by `DayCycle.advance`/`skip_phase`, then read `label()` for the
HUD or the `weekday`/`day_of_month`/`month`/`year` properties. `to_state()` /
`load_state()` persist the day count across restarts (see CompanyLink).
"""
from __future__ import annotations

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

DAYS_PER_MONTH = 30
MONTHS_PER_YEAR = len(MONTHS)
DAYS_PER_YEAR = DAYS_PER_MONTH * MONTHS_PER_YEAR


class GameCalendar:
    """Counts whole in-game days and reports them as a calendar date."""

    def __init__(self, day: int = 0) -> None:
        self.day = max(0, int(day))   # days elapsed since Year 1, the 1st

    def advance(self, rollovers: int) -> int:
        """Add `rollovers` new days — the int `DayCycle.advance`/`skip_phase`
        return each frame. Returns how many days were actually added (>= 0)."""
        n = max(0, int(rollovers or 0))
        self.day += n
        return n

    @property
    def weekday(self) -> str:
        return WEEKDAYS[self.day % len(WEEKDAYS)]

    @property
    def year(self) -> int:
        return self.day // DAYS_PER_YEAR + 1

    @property
    def month(self) -> int:
        """1-based month within the year (1..12)."""
        return (self.day % DAYS_PER_YEAR) // DAYS_PER_MONTH + 1

    @property
    def month_name(self) -> str:
        return MONTHS[self.month - 1]

    @property
    def day_of_month(self) -> int:
        """1-based day within the month (1..30)."""
        return self.day % DAYS_PER_MONTH + 1

    @property
    def day_number(self) -> int:
        """1-based running day count (Day 1 on a fresh game)."""
        return self.day + 1

    def label(self) -> str:
        """Full HUD date, e.g. 'Mon, Mar 12 · Yr 1'."""
        return f"{self.weekday}, {self.month_name} {self.day_of_month} · Yr {self.year}"

    def short(self) -> str:
        """Compact date, e.g. 'Day 42'."""
        return f"Day {self.day_number}"

    # --- persistence -------------------------------------------------------
    def to_state(self) -> int:
        return self.day

    def load_state(self, day) -> None:
        try:
            self.day = max(0, int(day))
        except (TypeError, ValueError):
            self.day = 0

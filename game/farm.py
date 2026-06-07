"""An idle South-America farm — passive income you grow and collect.

Reached through the Trade Embassy (a city building). The fantasy: your company
backs an overseas farming venture; you buy plots of cash crops, each trickles a
steady $/sec into an uncollected pot, and you swing by to collect. Income keeps
accruing while you're away (capped), so coming back to a "while you were away"
harvest is the sticky idle moment — same shape as the idle market (game/market.py).

Pure logic + persistence (no rendering; see game/farm_panel.py). Cash lives on the
Game (self.cash); buy() returns the cost to spend and collect() the amount to
credit, so money has a single source of truth.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

OFFLINE_CAP_SECONDS = 3 * 3600   # cap "while you were away" harvest at 3 hours


@dataclass(frozen=True)
class Crop:
    id: str
    name: str
    blurb: str
    cost0: int       # cost of your FIRST plot of this crop
    rate: float      # passive income per second, per plot owned
    growth: float    # cost multiplier per additional plot (the idle curve)


# The catalogue — a South-American cash-crop ladder. Higher tiers cost more up
# front but pay a slightly better $/sec per dollar invested, so climbing pays off.
CROPS: list[Crop] = [
    Crop("coffee", "Coffee Plantation",
         "Colombian high-altitude beans. The reliable first crop.",
         cost0=400, rate=1.0, growth=1.15),
    Crop("banana", "Banana Grove",
         "Ecuadorian plantations. Steady bunches, steady cash.",
         cost0=1_200, rate=3.5, growth=1.15),
    Crop("sugar", "Sugarcane Field",
         "Brazilian cane — sugar and ethanol both sell.",
         cost0=4_000, rate=12.0, growth=1.16),
    Crop("cattle", "Cattle Ranch",
         "Argentine pampas beef. Big land, bigger margins.",
         cost0=12_000, rate=40.0, growth=1.17),
    Crop("soy", "Soy Megafarm",
         "Endless Brazilian soy for the export market. The money crop.",
         cost0=40_000, rate=140.0, growth=1.18),
]
CROP_BY_ID = {c.id: c for c in CROPS}


@dataclass
class Farm:
    counts: dict           # crop_id -> plots owned
    accrued: float = 0.0   # uncollected income waiting in the pot
    last_seen: float = 0.0     # wall-clock of last save (for offline catch-up)
    away: dict | None = None   # last "while you were away" summary (consumed by UI)

    # ---- construction / persistence --------------------------------------
    @classmethod
    def fresh(cls) -> "Farm":
        return cls(counts={c.id: 0 for c in CROPS})

    @classmethod
    def load(cls, link) -> "Farm":
        """Restore from the save, else a fresh farm, then apply an offline harvest
        for time spent away."""
        f = cls.fresh()
        try:
            raw = link.load_farm()
        except Exception:
            raw = None
        if raw:
            for cid, n in raw.get("counts", {}).items():
                if cid in f.counts:
                    f.counts[cid] = int(n)
            f.accrued = float(raw.get("accrued", 0.0))
            f.last_seen = float(raw.get("last_seen", 0.0))
        f._catch_up()
        return f

    def to_dict(self) -> dict:
        return {"counts": self.counts, "accrued": self.accrued, "last_seen": time.time()}

    def save(self, link) -> None:
        try:
            link.save_farm(self.to_dict())
        except Exception:
            pass

    # ---- the income model ------------------------------------------------
    def rate(self) -> float:
        """Total passive income per second across every plot owned."""
        return sum(self.counts.get(c.id, 0) * c.rate for c in CROPS)

    def owned(self, crop_id: str) -> int:
        return int(self.counts.get(crop_id, 0))

    def cost(self, crop_id: str) -> int:
        """Cost of the NEXT plot of this crop (rises with how many you own)."""
        c = CROP_BY_ID.get(crop_id)
        if c is None:
            return 0
        return int(round(c.cost0 * (c.growth ** self.owned(crop_id))))

    def update(self, dt: float) -> None:
        """Accrue income for real time `dt` (call every frame, any game mode)."""
        self.accrued += self.rate() * dt

    def _catch_up(self) -> None:
        """Harvest income for wall-clock time away (capped); stash a UI summary."""
        now = time.time()
        if self.last_seen <= 0 or now <= self.last_seen:
            self.last_seen = now
            return
        elapsed = min(now - self.last_seen, OFFLINE_CAP_SECONDS)
        self.last_seen = now
        gained = self.rate() * elapsed
        if gained >= 1.0:
            self.accrued += gained
            self.away = {"gained": gained, "hours": elapsed / 3600.0}

    # ---- actions (return the cash delta; caller applies it to Game.cash) --
    def buy(self, crop_id: str) -> int:
        """Add one plot of `crop_id`. Returns the cost to charge (caller checks
        affordability first)."""
        if crop_id not in self.counts:
            return 0
        cost = self.cost(crop_id)
        self.counts[crop_id] = self.owned(crop_id) + 1
        return cost

    def collect(self) -> int:
        """Bank the whole accrued pot. Returns the (whole-dollar) amount."""
        amt = int(self.accrued)
        self.accrued -= amt
        return amt

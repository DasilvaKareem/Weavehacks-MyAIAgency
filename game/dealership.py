"""Auto Mall: a showroom of "ghost" display cars the CEO can buy.

Driving is gated behind ownership — the CEO can't just hop into a car, they have
to purchase one first (the car door stays locked until then). This module is the
showroom: a row of translucent hologram cars, each a model + price, laid out on an
open lot. The player walks the lot, picks one they can afford, and buys it; the
purchased model becomes their drivable car (see DrivableCar / main._buy_car).

Raylib-free so it stays headlessly unit-testable, like vehicle.py / traffic.py.
The park owns the actual rendering (park.draw_vehicle with alpha) — this module is
just placement + the "what's on offer / what's nearest" data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# The lineup. (model GLB basename in assets/cars, display name, price in $). Priced
# low → high so there's an affordable starter and an aspirational hero car.
LINEUP = [
    ("NormalCar2", "City Hatch", 4_000),
    ("Taxi", "Ex-Taxi", 8_000),
    ("Cop", "Retired Cruiser", 16_000),
    ("SportsCar", "Apex GT", 42_000),
    ("SportsCar2", "Apex GT-R", 65_000),
]


@dataclass
class CarDeal:
    """One car on the showroom floor."""
    model: str            # GLB basename in assets/cars
    name: str             # display name on the price tag
    price: int            # cost in $
    x: float = 0.0        # world position on the lot
    z: float = 0.0
    yaw: float = 0.0      # base facing (the showroom slowly spins them on top of this)
    sold: bool = False    # True once bought — its ghost is replaced by the real car


class Dealership:
    """A row of ghost display cars centred on (origin_x, origin_z), spread along X."""

    def __init__(self, origin_x: float, origin_z: float, *,
                 spacing: float = 3.0, yaw: float = 270.0) -> None:
        self.x = origin_x
        self.z = origin_z
        n = len(LINEUP)
        self.cars: list[CarDeal] = []
        for i, (model, name, price) in enumerate(LINEUP):
            off = (i - (n - 1) / 2.0) * spacing       # centre the row on the lot
            self.cars.append(CarDeal(model, name, price,
                                     x=origin_x + off, z=origin_z, yaw=yaw))

    def nearest(self, px: float, pz: float, reach: float) -> CarDeal | None:
        """The unsold display car within `reach` of (px,pz), nearest first."""
        best, best_d = None, reach
        for c in self.cars:
            if c.sold:
                continue
            d = math.hypot(c.x - px, c.z - pz)
            if d < best_d:
                best, best_d = c, d
        return best

    def cheapest(self) -> CarDeal | None:
        """The lowest-priced car still on the floor (handy for hints)."""
        avail = [c for c in self.cars if not c.sold]
        return min(avail, key=lambda c: c.price) if avail else None


def can_afford(deal: CarDeal, cash: int) -> bool:
    return cash >= deal.price

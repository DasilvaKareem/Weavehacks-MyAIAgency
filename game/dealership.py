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


# --- Parking-lot layout (world units) ---------------------------------------
# The Auto Mall is a showroom BUILDING with a paved LOT of cars in front of it
# (to the south, +z, where the CEO walks up). The 5 buyable models fill the
# front rows as spinning ghost holograms; the rest of the grid is decorative
# "stock" so the lot reads as a real, full dealership rather than a bare row.
LOT_COLS = 3                          # parking columns across (X)
LOT_ROWS = 3                          # parking rows front-to-back (Z)
SLOT_W = 4.4                          # column pitch
SLOT_D = 5.4                          # row pitch
BUILDING_BACK = 10.5                  # showroom sits this far behind (−z of) lot centre
CAR_YAW = 180.0                       # parked cars nosed in toward the building (−z)
BUILDING_MODEL = "2Story_Sign.glb"    # storefront with a built-in sign board
DECOR_MODELS = ["NormalCar1", "Bus", "Ambulance", "SchoolBus"]   # filler stock


@dataclass
class CarDeal:
    """One buyable car on the showroom floor."""
    model: str            # GLB basename in assets/cars
    name: str             # display name on the price tag
    price: int            # cost in $
    x: float = 0.0        # world position on the lot
    z: float = 0.0
    yaw: float = 0.0      # base facing (the showroom slowly spins them on top of this)
    sold: bool = False    # True once bought — its ghost is replaced by the real car


@dataclass
class LotCar:
    """A decorative parked car filling out the lot — drawn solid, never for sale."""
    model: str
    x: float
    z: float
    yaw: float = CAR_YAW


class Dealership:
    """A showroom building + a paved lot of display cars, centred on the lot
    origin (origin_x, origin_z). The buyable lineup fills the front slots as
    spinning ghosts; remaining slots are decorative stock."""

    def __init__(self, origin_x: float, origin_z: float, *, yaw: float = CAR_YAW) -> None:
        self.x = origin_x        # lot CENTRE (the AUTO MALL banner floats here)
        self.z = origin_z
        # The showroom building, set at the back of the lot, facing the CEO (+z).
        self.building = (origin_x, origin_z - BUILDING_BACK, 0.0, BUILDING_MODEL)
        # Paved pad covering the rows + the building apron.
        self.pad = (origin_x, origin_z - SLOT_D / 2.0,
                    LOT_COLS * SLOT_W + 3.0, LOT_ROWS * SLOT_D + 4.0)

        def slot_pos(r: int, c: int) -> tuple[float, float]:
            x = origin_x + (c - (LOT_COLS - 1) / 2.0) * SLOT_W
            z = origin_z + SLOT_D - r * SLOT_D       # r=0 front (+z) .. back (−z)
            return x, z

        slots = [(r, c) for r in range(LOT_ROWS) for c in range(LOT_COLS)]  # front→back
        self.cars: list[CarDeal] = []     # buyable ghost display cars (front rows)
        for i, (model, name, price) in enumerate(LINEUP):
            x, z = slot_pos(*slots[i])
            self.cars.append(CarDeal(model, name, price, x=x, z=z, yaw=yaw))
        self.decor: list[LotCar] = []     # decorative stock filling the rest of the lot
        for j, (r, c) in enumerate(slots[len(LINEUP):]):
            x, z = slot_pos(r, c)
            self.decor.append(LotCar(DECOR_MODELS[j % len(DECOR_MODELS)], x, z, yaw))

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


# Fraction of list price you get back when selling a car at the Auto Mall.
RESALE_FRACTION = 0.6


def price_of(model: str) -> int:
    """List price for a model basename (0 if it's not in the lineup)."""
    return next((p for m, _n, p in LINEUP if m == model), 0)


def name_of(model: str) -> str:
    """Display name for a model basename (falls back to the basename itself)."""
    return next((n for m, n, _p in LINEUP if m == model), model)


def resale_value(model: str) -> int:
    """What selling this car back pays out."""
    return int(price_of(model) * RESALE_FRACTION)

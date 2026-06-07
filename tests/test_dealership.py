from __future__ import annotations

import unittest

from game import dealership
from game.dealership import (Dealership, can_afford, price_of, name_of,
                            resale_value)


class DealershipTest(unittest.TestCase):
    def setUp(self):
        self.lot = Dealership(0.0, 0.0)

    def test_one_buyable_slot_per_lineup_entry(self):
        self.assertEqual(len(self.lot.cars), len(dealership.LINEUP))

    def test_lot_filled_out_with_decorative_stock(self):
        # The grid (rows x cols) is fully filled: buyables + decorative stock.
        self.assertEqual(len(self.lot.cars) + len(self.lot.decor),
                         dealership.LOT_ROWS * dealership.LOT_COLS)
        self.assertTrue(self.lot.decor)                       # there is filler stock

    def test_lot_columns_centred_on_origin(self):
        # Every parking column is symmetric about the lot origin, so the mean X
        # of all cars (buyable + stock) sits on the origin.
        xs = [c.x for c in self.lot.cars] + [c.x for c in self.lot.decor]
        self.assertAlmostEqual(sum(xs) / len(xs), 0.0, places=6)

    def test_building_sits_behind_the_lot(self):
        bx, bz, _yaw, model = self.lot.building
        self.assertEqual(bx, 0.0)                             # centred on the lot
        self.assertLess(bz, 0.0)                              # set back behind (−z)
        self.assertTrue(model.endswith(".glb"))

    def test_priced_low_to_high(self):
        prices = [c.price for c in self.lot.cars]
        self.assertEqual(prices, sorted(prices))
        self.assertIs(self.lot.cheapest(), self.lot.cars[0])

    def test_nearest_picks_closest_within_reach(self):
        target = self.lot.cars[1]
        got = self.lot.nearest(target.x + 0.2, target.z, reach=1.0)
        self.assertIs(got, target)

    def test_nearest_returns_none_out_of_reach(self):
        self.assertIsNone(self.lot.nearest(1000.0, 1000.0, reach=3.0))

    def test_sold_cars_are_skipped(self):
        target = self.lot.cars[0]
        target.sold = True
        # Standing right on the sold slot finds nothing (its ghost is gone).
        self.assertIsNone(self.lot.nearest(target.x, target.z, reach=0.5))
        self.assertIsNot(self.lot.cheapest(), target)

    def test_can_afford(self):
        car = self.lot.cars[2]
        self.assertTrue(can_afford(car, car.price))
        self.assertTrue(can_afford(car, car.price + 1))
        self.assertFalse(can_afford(car, car.price - 1))

    def test_lookup_helpers_by_model(self):
        car = self.lot.cars[3]
        self.assertEqual(price_of(car.model), car.price)
        self.assertEqual(name_of(car.model), car.name)

    def test_lookups_fall_back_for_unknown_model(self):
        self.assertEqual(price_of("NotACar"), 0)
        self.assertEqual(name_of("NotACar"), "NotACar")   # echoes the basename
        self.assertEqual(resale_value("NotACar"), 0)

    def test_resale_is_a_fraction_of_list(self):
        car = self.lot.cars[1]
        self.assertEqual(resale_value(car.model),
                         int(car.price * dealership.RESALE_FRACTION))
        self.assertLess(resale_value(car.model), car.price)   # selling loses value


if __name__ == "__main__":
    unittest.main()

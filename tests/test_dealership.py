from __future__ import annotations

import unittest

from game import dealership
from game.dealership import Dealership, can_afford


class DealershipTest(unittest.TestCase):
    def setUp(self):
        self.lot = Dealership(0.0, 0.0, spacing=3.0)

    def test_one_slot_per_lineup_entry(self):
        self.assertEqual(len(self.lot.cars), len(dealership.LINEUP))

    def test_row_is_centred_on_origin(self):
        xs = [c.x for c in self.lot.cars]
        self.assertAlmostEqual(sum(xs) / len(xs), 0.0, places=6)   # mean at origin
        self.assertTrue(all(c.z == 0.0 for c in self.lot.cars))

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


if __name__ == "__main__":
    unittest.main()

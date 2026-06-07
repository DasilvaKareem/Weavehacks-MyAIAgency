from __future__ import annotations

import math
import unittest

from game import vehicle
from game.vehicle import DrivableCar


def _drive(car, dt, secs, throttle=0.0, steer=0.0, handbrake=False):
    """Step the car for `secs` of sim at fixed `dt`."""
    for _ in range(int(secs / dt)):
        car.update(dt, throttle, steer, handbrake)


class VehicleTest(unittest.TestCase):
    def test_accelerates_forward_along_plus_z(self):
        # yaw=0 faces +Z; flooring it should build positive speed and roll +Z only.
        car = DrivableCar(x=0.0, z=0.0, yaw=0.0)
        _drive(car, 1 / 60, 1.0, throttle=1.0)
        self.assertGreater(car.speed, 0.0)
        self.assertGreater(car.z, 0.0)
        self.assertAlmostEqual(car.x, 0.0, places=6)

    def test_speed_clamped_to_max(self):
        car = DrivableCar(yaw=0.0)
        _drive(car, 1 / 60, 30.0, throttle=1.0)   # hold the throttle a long time
        self.assertLessEqual(car.speed, vehicle.MAX_SPEED + 1e-6)
        self.assertGreater(car.speed, vehicle.MAX_SPEED - 0.5)

    def test_brake_then_reverse(self):
        car = DrivableCar(yaw=0.0)
        _drive(car, 1 / 60, 1.0, throttle=1.0)    # get rolling forward
        _drive(car, 1 / 60, 3.0, throttle=-1.0)   # brake, then back up
        self.assertLess(car.speed, 0.0)
        self.assertGreaterEqual(car.speed, -vehicle.REVERSE_SPEED - 1e-6)

    def test_coast_decays_to_stop(self):
        car = DrivableCar(yaw=0.0)
        _drive(car, 1 / 60, 1.0, throttle=1.0)
        _drive(car, 1 / 60, 10.0, throttle=0.0)   # let drag bring it to rest
        self.assertAlmostEqual(car.speed, 0.0, places=4)

    def test_no_steering_while_stopped(self):
        car = DrivableCar(yaw=0.0)
        yaw0 = car.yaw
        _drive(car, 1 / 60, 1.0, throttle=0.0, steer=1.0)   # parked, full lock
        self.assertAlmostEqual(car.yaw, yaw0, places=6)

    def test_steering_turns_while_moving(self):
        car = DrivableCar(yaw=0.0)
        _drive(car, 1 / 60, 1.0, throttle=1.0)              # build speed first
        yaw0 = car.yaw
        _drive(car, 1 / 60, 0.5, throttle=1.0, steer=1.0)   # turn right
        self.assertNotAlmostEqual(car.yaw, yaw0, places=3)

    def test_steering_inverts_in_reverse(self):
        # Same steer input yields opposite yaw change going backward vs forward.
        fwd = DrivableCar(yaw=0.0)
        fwd.speed = 5.0
        fwd.update(1 / 60, 0.0, 1.0)
        rev = DrivableCar(yaw=0.0)
        rev.speed = -5.0
        rev.update(1 / 60, 0.0, 1.0)
        d_fwd = vehicle._sign(((fwd.yaw + 180) % 360) - 180)
        d_rev = vehicle._sign(((rev.yaw + 180) % 360) - 180)
        self.assertEqual(d_fwd, -d_rev)

    def test_handbrake_bleeds_speed(self):
        car = DrivableCar(yaw=0.0)
        _drive(car, 1 / 60, 1.0, throttle=1.0)
        fast = car.speed
        _drive(car, 1 / 60, 0.3, throttle=1.0, handbrake=True)
        self.assertLess(car.speed, fast)

    def test_heading_matches_yaw_convention(self):
        car = DrivableCar(yaw=90.0)               # nose toward +X
        hx, hz = car.heading()
        self.assertAlmostEqual(hx, 1.0, places=5)
        self.assertAlmostEqual(hz, 0.0, places=5)


if __name__ == "__main__":
    unittest.main()

"""Drivable car: arcade vehicle physics for the CEO to take the wheel in the park.

The ambient city traffic (traffic.py) drives the road grid on rails — axis-aligned
headings, no player input. This is the opposite: one car the CEO climbs into and
steers freely. The model is deliberately arcade (no tyre slip / suspension): a
single signed `speed` along the car's heading, throttle/brake/reverse on one axis,
and speed-sensitive steering so the car turns only while rolling and reverses its
steering when backing up.

Yaw matches the traffic + park convention exactly: yaw is degrees about +Y with
yaw=0 facing +Z, so heading = (sin(yaw), cos(yaw)). That lets the park render this
car through the same GLB path as ambient traffic (park.draw_vehicle).

Raylib-free so it stays headlessly unit-testable, like locomotion.py / traffic.py.
"""
from __future__ import annotations

import math

# Tunables (world units = metres, seconds). Tuned to feel brisk in the ~16-unit
# city blocks without being twitchy.
MAX_SPEED = 18.0           # top forward speed
REVERSE_SPEED = 6.0        # top speed in reverse
ACCEL = 14.0               # throttle pickup
BRAKE = 34.0               # foot-brake decel when throttle opposes motion
REVERSE_ACCEL = 9.0        # pickup once stopped and still holding reverse
DRAG = 6.0                 # rolling resistance / engine braking with no throttle
HANDBRAKE_DECEL = 26.0     # extra decel while the handbrake is held
TURN_RATE = 96.0           # degrees/sec of yaw change at full steer, full grip
STEER_FULL_SPEED = 7.0     # speed at which steering reaches full authority
CREEP_SPEED = 0.05         # below this |speed| the car is treated as stopped


def _sign(v: float) -> float:
    return (v > 0.0) - (v < 0.0)


class DrivableCar:
    """A single player-steered car. `model` names a GLB in assets/cars (drawn by
    the park); the physics here only own x/z/yaw/speed."""

    def __init__(self, x: float = 0.0, z: float = 0.0, yaw: float = 0.0,
                 model: str = "SportsCar") -> None:
        self.x = x
        self.z = z
        self.y = 0.0           # set by the park each frame so it rides the terrain
        self.yaw = yaw         # degrees about +Y, yaw=0 faces +Z
        self.speed = 0.0       # signed world units/sec along the heading
        self.model = model

    # --- queries -----------------------------------------------------------

    def heading(self) -> tuple[float, float]:
        """Unit ground vector the nose points along (matches yaw convention)."""
        rad = math.radians(self.yaw)
        return math.sin(rad), math.cos(rad)

    @property
    def moving(self) -> bool:
        return abs(self.speed) > CREEP_SPEED

    # --- per-frame ---------------------------------------------------------

    def update(self, dt: float, throttle: float, steer: float,
               handbrake: bool = False) -> None:
        """Advance the car one step.

        `throttle` in [-1, 1]: +forward, -brake-then-reverse. `steer` in [-1, 1]:
        +right, -left. `handbrake` bleeds speed fast regardless of throttle.
        """
        throttle = max(-1.0, min(1.0, throttle))
        steer = max(-1.0, min(1.0, steer))

        # --- longitudinal: one signed speed along the heading ---------------
        if throttle > 0.0:
            self.speed += ACCEL * throttle * dt
        elif throttle < 0.0:
            if self.speed > CREEP_SPEED:
                self.speed += BRAKE * throttle * dt        # braking (throttle<0)
            else:
                self.speed += REVERSE_ACCEL * throttle * dt  # roll into reverse
        else:
            # Coast: rolling resistance pulls speed toward zero without overshoot.
            self.speed -= _sign(self.speed) * min(abs(self.speed), DRAG * dt)

        if handbrake:
            self.speed -= _sign(self.speed) * min(abs(self.speed), HANDBRAKE_DECEL * dt)

        self.speed = max(-REVERSE_SPEED, min(MAX_SPEED, self.speed))

        # --- steering: only bites while rolling; flips when reversing --------
        if abs(self.speed) > CREEP_SPEED:
            grip = min(1.0, abs(self.speed) / STEER_FULL_SPEED)
            turn = steer * TURN_RATE * grip * dt
            if self.speed < 0.0:                # steering inverts in reverse
                turn = -turn
            self.yaw = (self.yaw + turn) % 360.0

        # --- integrate position ---------------------------------------------
        hx, hz = self.heading()
        self.x += hx * self.speed * dt
        self.z += hz * self.speed * dt

    def stop(self) -> None:
        """Cut the throttle and kill momentum (used on exit / teleport)."""
        self.speed = 0.0

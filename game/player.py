"""Player controller for the CEO character.

WASD moves relative to the camera, Shift sprints, Space jumps. Animation state
is derived from motion (Idle / Walk / Run / Jump) and written to the wrapped
Character, which already knows how to advance the clip.
"""
from __future__ import annotations

import math
import pyray as pr

from . import config, gamepad, locomotion, seating

# Tunables. Speeds, facing and bounds now live in locomotion (shared with bots);
# only the player-specific jump/gravity stay here.
WALK_SPEED = locomotion.WALK_SPEED
RUN_SPEED = locomotion.RUN_SPEED
GRAVITY = -22.0
JUMP_SPEED = 8.0


class Player:
    def __init__(self, character) -> None:
        self.ch = character
        self.vy = 0.0
        self.grounded = True
        self.seated = False   # CEO sitting (press C); any movement stands up
        self.seat = None      # (x, z, yaw) seat snapped onto, or None for sit-in-place

    def update(self, dt: float, camera, others=None) -> None:
        fx, fz = camera.forward_xz()
        rx, rz = camera.right_xz()

        # Keyboard (digital) blended with the left stick (analog). Stick Y is
        # negative when pushed up, which is "forward", so we negate it.
        gx, gy = gamepad.left_stick()
        ix = (pr.is_key_down(pr.KEY_D) - pr.is_key_down(pr.KEY_A)) + gx
        # W = forward (away from camera) via forward_xz; S = back. Stick Y is
        # negative when pushed up (forward), so negate it.
        iz = (pr.is_key_down(pr.KEY_W) - pr.is_key_down(pr.KEY_S)) - gy
        # Clamp the input vector to the unit disc so diagonals/blends aren't faster.
        imag = math.hypot(ix, iz)
        if imag > 1.0:
            ix, iz = ix / imag, iz / imag
            imag = 1.0

        mx = fx * iz + rx * ix
        mz = fz * iz + rz * ix
        mag = math.hypot(mx, mz)
        moving = mag > 1e-4

        running = (pr.is_key_down(pr.KEY_LEFT_SHIFT) or pr.is_key_down(pr.KEY_RIGHT_SHIFT)
                   or gamepad.down(gamepad.R2))
        speed = RUN_SPEED if running else WALK_SPEED

        jump = pr.is_key_pressed(pr.KEY_SPACE) or gamepad.pressed(gamepad.CROSS)

        # --- sit (press C) ----------------------------------------------
        # Toggle sitting. On sit-down, snap onto the nearest free seat (meeting
        # stool / lounge couch) and face it; with none in reach, just sit in
        # place. Any movement or a jump stands the CEO back up.
        if pr.is_key_pressed(pr.KEY_C) or gamepad.pressed(gamepad.CIRCLE):
            if self.seated:
                self.seated, self.seat = False, None
            else:
                occupied = [(o.x, o.z) for o in (others or ()) if o is not self.ch]
                self.seat = seating.nearest_seat(self.ch.x, self.ch.z, occupied)
                self.seated = True
                if self.seat is not None:
                    self.ch.x, self.ch.z, self.ch.yaw = self.seat
        if self.seated and (moving or jump):
            self.seated, self.seat = False, None
        if self.seated:
            self.ch.y, self.vy, self.grounded = 0.0, 0.0, True
            locomotion.apply_anim(self.ch, moving=False, seated=True)
            return

        if moving:
            # Direction is normalized for facing; analog tilt scales the speed so
            # a light stick push walks slowly and a full push hits top speed.
            mx, mz = mx / mag, mz / mag
            self.ch.x += mx * speed * imag * dt
            self.ch.z += mz * speed * imag * dt
            self.ch.x, self.ch.z = locomotion.clamp_to_bounds(self.ch.x, self.ch.z)
            locomotion.face_dir(self.ch, mx, mz, dt)   # smoothed turn toward motion

        # --- jump / gravity ---------------------------------------------
        if self.grounded and jump:
            self.vy = JUMP_SPEED
            self.grounded = False
        self.vy += GRAVITY * dt
        self.ch.y += self.vy * dt
        if self.ch.y <= 0.0:
            self.ch.y = 0.0
            self.vy = 0.0
            self.grounded = True

        # --- animation state --------------------------------------------
        locomotion.apply_anim(self.ch, moving=moving, running=running,
                              grounded=self.grounded)

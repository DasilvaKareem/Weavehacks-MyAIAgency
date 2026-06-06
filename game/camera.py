"""Third-person follow camera.

Orbits a moving target. Hold the RIGHT mouse button to look around (cursor is
hidden while held); mouse wheel zooms. The camera smoothly trails the target so
movement feels weighty rather than rigid.
"""
from __future__ import annotations

import math
import pyray as pr

from . import gamepad

# Tunables
MOUSE_SENS = 0.005          # radians per pixel of mouse movement
KEY_ROTATE_SPEED = 1.8      # radians/sec for Q/E fallback
PAD_LOOK_SPEED = 2.6        # radians/sec at full right-stick deflection
PAD_ZOOM_SPEED = 12.0       # distance units/sec while holding L1/R1
PITCH_MIN = math.radians(-10.0)
PITCH_MAX = math.radians(70.0)
DIST_MIN = 4.0
DIST_MAX = 18.0
FOLLOW_LERP = 8.0           # higher = snappier follow
TARGET_HEIGHT = 1.4         # look at roughly chest height of the player


class ThirdPersonCamera:
    def __init__(self, target_xyz: tuple[float, float, float]) -> None:
        self.yaw = math.radians(0.0)     # orbit angle around target
        self.pitch = math.radians(25.0)
        self.distance = 9.0
        self._focus = pr.Vector3(target_xyz[0], target_xyz[1] + TARGET_HEIGHT, target_xyz[2])
        self.camera = pr.Camera3D(
            pr.Vector3(0, 5, 10), self._focus, pr.Vector3(0, 1, 0),
            45.0, pr.CAMERA_PERSPECTIVE,
        )
        self._looking = False

    def recenter(self, target) -> None:
        """Snap the orbit back behind the player at the default pitch/zoom."""
        # player.yaw tracks the camera yaw while walking forward, so matching it
        # puts the camera behind the player, looking the way they face.
        self.yaw = math.radians(target.yaw)
        self.pitch = math.radians(25.0)
        self.distance = 9.0

    def update(self, dt: float, target) -> None:
        if gamepad.pressed(gamepad.R3):
            self.recenter(target)

        # --- look controls -------------------------------------------------
        if pr.is_mouse_button_down(pr.MOUSE_BUTTON_RIGHT):
            if not self._looking:
                pr.disable_cursor()
                self._looking = True
            d = pr.get_mouse_delta()
            self.yaw -= d.x * MOUSE_SENS
            self.pitch += d.y * MOUSE_SENS
        elif self._looking:
            pr.enable_cursor()
            self._looking = False

        # keyboard fallback so it's usable without holding the mouse
        if pr.is_key_down(pr.KEY_Q):
            self.yaw += KEY_ROTATE_SPEED * dt
        if pr.is_key_down(pr.KEY_E):
            self.yaw -= KEY_ROTATE_SPEED * dt

        # right stick orbits (no button to hold, unlike the mouse)
        sx, sy = gamepad.right_stick()
        self.yaw -= sx * PAD_LOOK_SPEED * dt
        self.pitch += sy * PAD_LOOK_SPEED * dt

        self.pitch = max(PITCH_MIN, min(PITCH_MAX, self.pitch))
        self.distance -= pr.get_mouse_wheel_move()
        # L1 zooms out, R1 zooms in (held)
        self.distance += (gamepad.down(gamepad.L1) - gamepad.down(gamepad.R1)) * PAD_ZOOM_SPEED * dt
        self.distance = max(DIST_MIN, min(DIST_MAX, self.distance))

        # --- smooth follow -------------------------------------------------
        desired = pr.Vector3(target.x, target.y + TARGET_HEIGHT, target.z)
        t = min(1.0, FOLLOW_LERP * dt)
        self._focus.x += (desired.x - self._focus.x) * t
        self._focus.y += (desired.y - self._focus.y) * t
        self._focus.z += (desired.z - self._focus.z) * t

        # --- position from yaw/pitch/distance ------------------------------
        cp = math.cos(self.pitch)
        ox = math.sin(self.yaw) * cp * self.distance
        oz = math.cos(self.yaw) * cp * self.distance
        oy = math.sin(self.pitch) * self.distance
        self.camera.position = pr.Vector3(
            self._focus.x + ox, self._focus.y + oy, self._focus.z + oz
        )
        self.camera.target = self._focus

    def forward_xz(self) -> tuple[float, float]:
        """Unit ground vector pointing where the camera looks (W direction)."""
        return (-math.sin(self.yaw), -math.cos(self.yaw))

    def right_xz(self) -> tuple[float, float]:
        """Unit ground vector to the camera's right (D direction)."""
        fx, fz = self.forward_xz()
        return (-fz, fx)

"""DualSense (PS5) / generic gamepad helpers.

raylib maps controllers through GLFW, so a DualSense reports the standard layout:
left stick = move, right stick = look, Cross = jump, Square = hire, R2 = sprint,
L1/R1 = zoom. Everything degrades gracefully — if no pad is plugged in, every
reader returns 0 / False and the keyboard + mouse path is unaffected.

Stick axes are noisy at rest, so reads pass through a radial dead zone.
"""
from __future__ import annotations

import math
import pyray as pr

PAD = 0                     # first connected controller
STICK_DEADZONE = 0.18       # ignore drift below this magnitude
TRIGGER_THRESHOLD = 0.5     # analog trigger counts as "pressed" past this

# Semantic button aliases (PS5 face/shoulder names -> raylib enum).
CROSS = pr.GAMEPAD_BUTTON_RIGHT_FACE_DOWN
CIRCLE = pr.GAMEPAD_BUTTON_RIGHT_FACE_RIGHT    # back / close
SQUARE = pr.GAMEPAD_BUTTON_RIGHT_FACE_LEFT
TRIANGLE = pr.GAMEPAD_BUTTON_RIGHT_FACE_UP    # talk to the selected agent
L1 = pr.GAMEPAD_BUTTON_LEFT_TRIGGER_1
R1 = pr.GAMEPAD_BUTTON_RIGHT_TRIGGER_1
R2 = pr.GAMEPAD_BUTTON_RIGHT_TRIGGER_2
R3 = pr.GAMEPAD_BUTTON_RIGHT_THUMB          # right stick click
DPAD_LEFT = pr.GAMEPAD_BUTTON_LEFT_FACE_LEFT
DPAD_RIGHT = pr.GAMEPAD_BUTTON_LEFT_FACE_RIGHT
DPAD_UP = pr.GAMEPAD_BUTTON_LEFT_FACE_UP        # open the shop
DPAD_DOWN = pr.GAMEPAD_BUTTON_LEFT_FACE_DOWN


def available() -> bool:
    return pr.is_gamepad_available(PAD)


def _raw(axis: int) -> float:
    return pr.get_gamepad_axis_movement(PAD, axis) if available() else 0.0


def stick(x_axis: int, y_axis: int) -> tuple[float, float]:
    """Return a dead-zoned (x, y) for a stick, rescaled so motion starts cleanly
    at the dead-zone edge (no jump from 0 to 0.18). Magnitude is clamped to 1."""
    x, y = _raw(x_axis), _raw(y_axis)
    mag = math.hypot(x, y)
    if mag <= STICK_DEADZONE:
        return 0.0, 0.0
    scaled = min(1.0, (mag - STICK_DEADZONE) / (1.0 - STICK_DEADZONE)) / mag
    return x * scaled, y * scaled


def left_stick() -> tuple[float, float]:
    return stick(pr.GAMEPAD_AXIS_LEFT_X, pr.GAMEPAD_AXIS_LEFT_Y)


def right_stick() -> tuple[float, float]:
    return stick(pr.GAMEPAD_AXIS_RIGHT_X, pr.GAMEPAD_AXIS_RIGHT_Y)


def down(button: int) -> bool:
    return available() and pr.is_gamepad_button_down(PAD, button)


def pressed(button: int) -> bool:
    return available() and pr.is_gamepad_button_pressed(PAD, button)

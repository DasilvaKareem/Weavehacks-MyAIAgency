"""The title / home screen — the first thing you see on launch.

A clean menu over a soft gradient: New World (start fresh — runs the prologue),
Continue (resume the saved company; disabled when there's no save), and Quit.
Starting a New World over an existing save asks to confirm first, since it
archives the current company.

Self-contained like the other full-screen screens: it runs its own
begin/end_drawing and returns an action string once the player picks one:
"new" | "continue" | "quit", else None while still on the menu.
"""
from __future__ import annotations

import pyray as pr

from .ui import _panel_button

_TOP = pr.Color(20, 24, 36, 255)
_BOTTOM = pr.Color(10, 12, 20, 255)
_ACCENT = pr.Color(70, 130, 220, 255)
_GREEN = pr.Color(45, 140, 80, 255)
_GREY = pr.Color(54, 60, 78, 255)
_DIM = pr.Color(120, 130, 150, 255)


class MainMenu:
    def __init__(self) -> None:
        self._confirm = False        # New World confirm overlay (when a save exists)
        self._t = 0.0                # gentle title bob

    def draw(self, has_save: bool) -> str | None:
        dt = pr.get_frame_time()
        self._t += dt
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        mouse = pr.get_mouse_position()

        pr.begin_drawing()
        pr.draw_rectangle_gradient_v(0, 0, sw, sh, _TOP, _BOTTOM)

        # Title + tagline (centered upper third).
        title = "COMPANY.AI"
        tw = pr.measure_text(title, 84)
        ty = int(sh * 0.20)
        pr.draw_text(title, sw // 2 - tw // 2 + 3, ty + 3, 84, pr.Color(0, 0, 0, 120))
        pr.draw_text(title, sw // 2 - tw // 2, ty, 84, pr.RAYWHITE)
        pr.draw_rectangle(sw // 2 - tw // 2, ty + 92, tw, 4, _ACCENT)
        tag = "From one rejected idea to an empire."
        gw = pr.measure_text(tag, 22)
        pr.draw_text(tag, sw // 2 - gw // 2, ty + 108, 22, _DIM)

        action = None
        if self._confirm:
            action = self._draw_confirm(sw, sh, mouse)
        else:
            action = self._draw_buttons(sw, sh, mouse, has_save)

        ver = "v0.1  ·  prototype"
        pr.draw_text(ver, 18, sh - 30, 16, _DIM)
        pr.end_drawing()
        return action

    def _draw_buttons(self, sw, sh, mouse, has_save) -> str | None:
        bw, bh, gap = 320, 58, 18
        bx = sw // 2 - bw // 2
        by = int(sh * 0.52)

        if _panel_button(pr.Rectangle(bx, by, bw, bh), "New World", _GREEN, mouse):
            if has_save:
                self._confirm = True
            else:
                return "new"

        cont = pr.Rectangle(bx, by + (bh + gap), bw, bh)
        if has_save:
            if _panel_button(cont, "Continue", _ACCENT, mouse) or pr.is_key_pressed(pr.KEY_ENTER):
                return "continue"
        else:
            pr.draw_rectangle_rec(cont, _GREY)               # disabled look
            label = "Continue"
            lw = pr.measure_text(label, 22)
            pr.draw_text(label, int(cont.x + (bw - lw) / 2), int(cont.y + 16), 22, _DIM)
            pr.draw_text("no saved company", int(cont.x + (bw - pr.measure_text("no saved company", 14)) / 2),
                         int(cont.y + bh - 2), 14, _DIM)

        if _panel_button(pr.Rectangle(bx, by + 2 * (bh + gap), bw, bh), "Quit", _GREY, mouse) \
                or pr.is_key_pressed(pr.KEY_ESCAPE):
            return "quit"
        return None

    def _draw_confirm(self, sw, sh, mouse) -> str | None:
        pr.draw_rectangle(0, 0, sw, sh, pr.Color(0, 0, 0, 170))
        w, h = 480, 210
        x, y = (sw - w) // 2, (sh - h) // 2
        pr.draw_rectangle(x, y, w, h, pr.Color(28, 32, 46, 255))
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 2, _ACCENT)
        pr.draw_text("Start a New World?", x + 26, y + 22, 28, pr.RAYWHITE)
        for i, line in enumerate(["This sets your current company aside and",
                                  "begins a brand-new one from scratch."]):
            pr.draw_text(line, x + 26, y + 66 + i * 24, 18, _DIM)
        if _panel_button(pr.Rectangle(x + 26, y + h - 64, 200, 46), "New World", _GREEN, mouse):
            self._confirm = False
            return "new"
        if _panel_button(pr.Rectangle(x + w - 166, y + h - 64, 140, 46), "Back", _GREY, mouse) \
                or pr.is_key_pressed(pr.KEY_ESCAPE):
            self._confirm = False
        return None

"""The South-America farm terminal — the idle-farm UI (see game/farm.py for logic).

Looks like a little farm-management board: income/sec up top, a harvest banner you
collect from, and a row per crop with its rate, plots owned, and a Buy button. A
"while you were away" card shows first when you return to a grown pot.

draw(farm, cash) renders the modal and RETURNS an action tuple for the Game to
apply to its cash (single source of truth for money):
  ("buy", crop_id) | ("collect", None)
or None. Esc closes.
"""
from __future__ import annotations

import pyray as pr

from . import farm as farm_mod

_DIM = pr.Color(0, 0, 0, 180)
_BOX = pr.Color(20, 30, 22, 252)
_BAR = pr.Color(28, 44, 32, 255)
_GOLD = pr.Color(232, 196, 96, 255)
_GREEN = pr.Color(120, 205, 110, 255)
_LEAF = pr.Color(80, 175, 90, 255)
_RED = pr.Color(225, 110, 110, 255)
_ACCENT = pr.Color(150, 200, 110, 255)
_INK = pr.RAYWHITE
_DIMTX = pr.Color(150, 170, 150, 255)
_BTN = pr.Color(54, 96, 60, 255)
_BTN_HOT = pr.Color(74, 128, 80, 255)
_BTN_OFF = pr.Color(40, 48, 42, 255)


class FarmPanel:
    def __init__(self) -> None:
        self.open = False

    @property
    def capturing(self) -> bool:
        return self.open

    def open_panel(self) -> None:
        self.open = True
        while pr.get_char_pressed() > 0:
            pass

    def close(self) -> None:
        self.open = False

    @staticmethod
    def _money(v: float) -> str:
        return f"${int(v):,}"

    def _button(self, x, y, w, h, label, enabled=True, size=15) -> bool:
        rect = pr.Rectangle(x, y, w, h)
        hot = enabled and pr.check_collision_point_rec(pr.get_mouse_position(), rect)
        col = _BTN_HOT if hot else (_BTN if enabled else _BTN_OFF)
        pr.draw_rectangle_rec(rect, col)
        tw = pr.measure_text(label, size)
        pr.draw_text(label, int(x + (w - tw) / 2), int(y + (h - size) / 2), size,
                     _INK if enabled else _DIMTX)
        return hot and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)

    def draw(self, farm: "farm_mod.Farm", cash: float):
        if not self.open:
            return None
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pr.draw_rectangle(0, 0, sw, sh, _DIM)
        w, h = 760, 560
        x, y = (sw - w) // 2, (sh - h) // 2
        pr.draw_rectangle(x, y, w, h, _BOX)
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 2, _ACCENT)

        if farm.away:
            return self._draw_away(farm, x, y, w, h)

        # header
        pr.draw_rectangle(x, y, w, 52, _BAR)
        pr.draw_text("South America — Company Farm", x + 18, y + 14, 24, _INK)
        cashs = f"Cash  {self._money(cash)}"
        pr.draw_text(cashs, x + w - pr.measure_text(cashs, 18) - 18, y + 8, 18, _GOLD)
        rate = farm.rate()
        rates = f"Income  {self._money(rate)}/sec"
        pr.draw_text(rates, x + w - pr.measure_text(rates, 15) - 18, y + 31, 15, _GREEN)

        action = None

        # harvest banner
        hy = y + 64
        pr.draw_rectangle(x + 16, hy, w - 32, 56, pr.Color(30, 50, 34, 255))
        pr.draw_text("Ready to harvest", x + 28, hy + 8, 16, _DIMTX)
        pot = f"{self._money(farm.accrued)}"
        pr.draw_text(pot, x + 28, hy + 28, 22, _GOLD)
        if self._button(x + w - 16 - 180, hy + 12, 180, 32,
                        "Collect", enabled=farm.accrued >= 1):
            action = ("collect", None)

        # crop rows
        cy = hy + 70
        for c in farm_mod.CROPS:
            act = self._crop_row(farm, cash, c, x + 16, cy, w - 32)
            action = act or action
            cy += 72

        pr.draw_text("Buy plots → passive $/sec. Income grows even while you're away. · Esc to leave",
                     x + 18, y + h - 26, 14, _DIMTX)
        if pr.is_key_pressed(pr.KEY_ESCAPE):
            self.close()
        return action

    def _crop_row(self, farm, cash, c, x, y, w):
        owned = farm.owned(c.id)
        pr.draw_rectangle(x, y, w, 64, pr.Color(28, 38, 30, 255))
        pr.draw_rectangle(x, y, 6, 64, _LEAF)
        pr.draw_text(c.name, x + 16, y + 8, 19, _INK)
        own = f"x{owned}" if owned else "—"
        pr.draw_text(own, x + 16 + pr.measure_text(c.name, 19) + 12, y + 11, 16, _GOLD)
        pr.draw_text(c.blurb, x + 16, y + 34, 13, _DIMTX)
        # per-plot rate + your current contribution
        rate = f"+{self._money(c.rate)}/s each"
        pr.draw_text(rate, x + 360, y + 10, 15, _GREEN)
        if owned:
            tot = f"now {self._money(owned * c.rate)}/s"
            pr.draw_text(tot, x + 360, y + 34, 13, _DIMTX)
        # buy button
        cost = farm.cost(c.id)
        bw, bh = 168, 36
        bx, by = x + w - bw - 12, y + 14
        if self._button(bx, by, bw, bh, f"Buy  {self._money(cost)}",
                        enabled=cash >= cost):
            return ("buy", c.id)
        return None

    def _draw_away(self, farm, x, y, w, h):
        info = farm.away
        pr.draw_text("While you were away", x + 24, y + 44, 30, _GOLD)
        gained = info.get("gained", 0.0)
        pr.draw_text(f"Your farm grew {self._money(gained)}", x + 24, y + 104, 24, _GREEN)
        sub = f"over {info.get('hours', 0):.1f} hours — waiting in the barn to collect"
        pr.draw_text(sub, x + 24, y + 142, 18, _DIMTX)
        pr.draw_text("Crops don't sleep. Reinvest the harvest and grow the empire.",
                     x + 24, y + 188, 16, _ACCENT)
        if (self._button(x + 24, y + h - 70, 220, 42, "Collect & continue")
                or pr.is_key_pressed(pr.KEY_ENTER) or pr.is_key_pressed(pr.KEY_ESCAPE)):
            farm.away = None
        return None

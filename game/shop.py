"""Office shop: browse the furniture catalog and buy items with company cash.

The catalog lives in assets/shop_items.json (data, not code), so adding an item
is a JSON edit — as long as its `kind` has a builder in furniture.build(). The
panel is self-contained: draw(cash) processes input and returns an action for
main.py to act on ('close', or ('buy', item)). It never touches the world or the
wallet directly — purchasing/placement is the Game's job.
"""
from __future__ import annotations

import json
import os

import pyray as pr

from . import gamepad

CATALOG_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "assets", "shop_items.json")

PANEL_W = 560
ROW_H = 46
PAD = 18

BG = pr.Color(22, 26, 36, 245)
BORDER = pr.Color(70, 130, 220, 255)
BAR = pr.Color(34, 92, 168, 255)
ROW = pr.Color(32, 38, 52, 255)
ROW_SEL = pr.Color(48, 64, 92, 255)
AFFORD = pr.Color(70, 200, 120, 255)
TOO_DEAR = pr.Color(200, 90, 90, 255)


def load_catalog(path: str = CATALOG_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["items"]


_TAB_LABELS = {"furniture": "Furniture", "floor": "Floors", "wall": "Walls", "door": "Doors"}


class ShopPanel:
    def __init__(self, items: list[dict]) -> None:
        self.items = items
        # Distinct categories, in first-seen order, drive the tabs.
        self.tabs: list[str] = []
        for it in items:
            cat = it.get("category", "furniture")
            if cat not in self.tabs:
                self.tabs.append(cat)
        self.open = False
        self.tab = 0
        self.sel = 0
        self.flash = ""           # transient "can't afford" style message

    def open_(self) -> None:
        self.open = True
        self.sel = 0
        self.flash = ""

    def close(self) -> None:
        self.open = False

    def _current(self) -> list[dict]:
        cat = self.tabs[self.tab]
        return [it for it in self.items if it.get("category", "furniture") == cat]

    def _set_tab(self, idx: int) -> None:
        self.tab = idx % len(self.tabs)
        self.sel = 0
        self.flash = ""

    def _panel_rect(self) -> tuple[int, int, int, int]:
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        rows = max(len(self._current()), 1)
        h = 70 + 34 + rows * ROW_H + 46           # title + tab bar + rows + footer
        return (sw - PANEL_W) // 2, (sh - h) // 2, PANEL_W, h

    def draw(self, cash: int):
        """Render + handle input. Returns None | 'close' | ('buy', item)."""
        if not self.open:
            return None
        x, y, w, h = self._panel_rect()
        mouse = pr.get_mouse_position()
        rows = self._current()
        n = len(rows)

        # --- input: tab + row navigation ---------------------------------
        if pr.is_key_pressed(pr.KEY_RIGHT) or gamepad.pressed(gamepad.R1):
            self._set_tab(self.tab + 1)
        if pr.is_key_pressed(pr.KEY_LEFT) or gamepad.pressed(gamepad.L1):
            self._set_tab(self.tab - 1)
        if pr.is_key_pressed(pr.KEY_DOWN) or gamepad.pressed(gamepad.DPAD_DOWN):
            self.sel = (self.sel + 1) % n
        if pr.is_key_pressed(pr.KEY_UP) or gamepad.pressed(gamepad.DPAD_UP):
            self.sel = (self.sel - 1) % n
        if pr.is_key_pressed(pr.KEY_ESCAPE) or gamepad.pressed(gamepad.CIRCLE):
            return "close"

        # --- backdrop + frame --------------------------------------------
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pr.draw_rectangle(0, 0, sw, sh, pr.Color(0, 0, 0, 130))
        pr.draw_rectangle(x, y, w, h, BG)
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 2, BORDER)
        pr.draw_rectangle(x, y, w, 44, BAR)
        pr.draw_text("Office Shop", x + PAD, y + 12, 22, pr.RAYWHITE)
        cash_txt = f"Cash  ${cash:,}"
        ctw = pr.measure_text(cash_txt, 20)
        pr.draw_text(cash_txt, x + w - PAD - ctw, y + 13, 20, pr.GOLD)

        # --- tab bar ------------------------------------------------------
        tx = x + PAD
        ty = y + 50
        for i, cat in enumerate(self.tabs):
            label = _TAB_LABELS.get(cat, cat.title())
            tw = pr.measure_text(label, 17)
            tab_rect = pr.Rectangle(tx, ty, tw + 22, 26)
            active = i == self.tab
            pr.draw_rectangle_rec(tab_rect, pr.Color(60, 130, 210, 255) if active else ROW)
            pr.draw_text(label, int(tx + 11), int(ty + 5), 17,
                         pr.RAYWHITE if active else pr.Color(170, 180, 200, 255))
            if pr.check_collision_point_rec(mouse, tab_rect) and \
                    pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
                self._set_tab(i)
                rows = self._current()
                n = len(rows)
            tx += tw + 30

        # --- rows ---------------------------------------------------------
        buy = None
        ry = y + 88
        is_paint = self.tabs[self.tab] != "furniture"
        for i, it in enumerate(rows):
            rect = pr.Rectangle(x + PAD, ry, w - 2 * PAD, ROW_H - 6)
            hover = pr.check_collision_point_rec(mouse, rect)
            if hover:
                self.sel = i
            affordable = cash >= it["price"]
            pr.draw_rectangle_rec(rect, ROW_SEL if i == self.sel else ROW)
            text_x = int(rect.x + 12)
            if is_paint and "color" in it:                # colour swatch
                c = it["color"]
                sw_rect = pr.Rectangle(rect.x + 10, rect.y + 8, 24, ROW_H - 22)
                pr.draw_rectangle_rec(sw_rect, pr.Color(int(c[0]), int(c[1]), int(c[2]), 255))
                pr.draw_rectangle_lines_ex(sw_rect, 1, pr.Color(0, 0, 0, 120))
                text_x = int(rect.x + 44)
            pr.draw_text(it["name"], text_x, int(rect.y + 4), 19, pr.RAYWHITE)
            pr.draw_text(it["desc"], text_x, int(rect.y + 25), 13, pr.Color(150, 165, 190, 255))
            price = f"${it['price']:,}"
            ptw = pr.measure_text(price, 19)
            pr.draw_text(price, int(rect.x + rect.width - 96 - ptw), int(rect.y + 12), 19,
                         AFFORD if affordable else TOO_DEAR)
            # Buy button
            bb = pr.Rectangle(rect.x + rect.width - 84, rect.y + 6, 76, ROW_H - 18)
            bhov = pr.check_collision_point_rec(mouse, bb)
            base = (pr.Color(45, 140, 80, 255) if affordable else pr.Color(70, 74, 84, 255))
            if affordable and bhov:
                base = pr.Color(60, 175, 100, 255)
            pr.draw_rectangle_rec(bb, base)
            label = "Paint" if is_paint else "Buy"
            bw = pr.measure_text(label, 18)
            pr.draw_text(label, int(bb.x + (bb.width - bw) / 2), int(bb.y + 6), 18, pr.RAYWHITE)
            if affordable and bhov and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
                buy = it
            ry += ROW_H

        # confirm selected via keyboard / controller
        if (pr.is_key_pressed(pr.KEY_ENTER) or gamepad.pressed(gamepad.CROSS)) and rows:
            it = rows[self.sel % n]
            if cash >= it["price"]:
                buy = it
            else:
                self.flash = "Not enough cash"

        # --- footer -------------------------------------------------------
        foot = self.flash or "L/R tabs  -  Up/Down select  -  Enter / X buy  -  Esc / O close"
        col = TOO_DEAR if self.flash else pr.LIGHTGRAY
        pr.draw_text(foot, x + PAD, y + h - 26, 14, col)

        if buy is not None:
            self.flash = ""
            return ("buy", buy)
        return None

"""Agent marketplace: browse and hire any character in the pack.

Catalog lives in assets/agents_catalog.json (one entry per gltf in
assets/models). Tabs group the roster (office / warriors / fantasy / critters);
each tab scrolls since some hold 20+ characters. The panel is pure UI — it
returns an action ('close' or ('hire', item)) and never touches the wallet or
the world; the Game commits the hire (which then runs the role/skin dialog).
"""
from __future__ import annotations

import json
import os

import pyray as pr

from . import gamepad

CATALOG_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "assets", "agents_catalog.json")

PANEL_W = 600
ROW_H = 42
VISIBLE = 9                 # rows shown at once before scrolling
PAD = 18

BG = pr.Color(22, 26, 36, 245)
BORDER = pr.Color(70, 130, 220, 255)
BAR = pr.Color(34, 92, 168, 255)
ROW = pr.Color(32, 38, 52, 255)
ROW_SEL = pr.Color(48, 64, 92, 255)
AFFORD = pr.Color(70, 200, 120, 255)
TOO_DEAR = pr.Color(200, 90, 90, 255)
LOCKED = pr.Color(190, 150, 60, 255)        # premium/locked accent (gold)
UNLOCK_BTN = pr.Color(150, 110, 40, 255)    # "Unlock" button base


def is_unlocked(item: dict, unlocked: set) -> bool:
    """A catalog outfit is usable when it isn't premium, or has been purchased."""
    return not item.get("locked") or item["id"] in unlocked


def _draw_lock(x: int, y: int, color) -> None:
    """A tiny padlock glyph (the default raylib font has no emoji)."""
    pr.draw_rectangle(x, y + 6, 12, 9, color)                       # body
    pr.draw_ring(pr.Vector2(x + 6, y + 6), 3, 5, 180, 360, 16, color)  # shackle

_TAB_LABELS = {"office": "Office", "warriors": "Warriors", "fantasy": "Fantasy", "critters": "Critters"}


def load_catalog(path: str = CATALOG_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["items"]


class MarketplacePanel:
    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.tabs: list[str] = []
        for it in items:
            c = it.get("category", "office")
            if c not in self.tabs:
                self.tabs.append(c)
        self.open = False
        self.tab = 0
        self.sel = 0
        self.top = 0          # scroll offset (index of first visible row)
        self.flash = ""

    def open_(self) -> None:
        self.open = True
        self.tab = 0
        self.sel = 0
        self.top = 0
        self.flash = ""

    def close(self) -> None:
        self.open = False

    def _current(self) -> list[dict]:
        cat = self.tabs[self.tab]
        return [it for it in self.items if it.get("category", "office") == cat]

    def _set_tab(self, idx: int) -> None:
        self.tab = idx % len(self.tabs)
        self.sel = 0
        self.top = 0
        self.flash = ""

    def _clamp_scroll(self, n: int) -> None:
        # keep the selection inside the visible window
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + VISIBLE:
            self.top = self.sel - VISIBLE + 1
        self.top = max(0, min(self.top, max(0, n - VISIBLE)))

    def _panel_rect(self) -> tuple[int, int, int, int]:
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        rows = min(len(self._current()), VISIBLE)
        h = 50 + 34 + rows * ROW_H + 42
        return (sw - PANEL_W) // 2, (sh - h) // 2, PANEL_W, h

    def draw(self, cash: int, unlocked: set | None = None):
        """Render + handle input.

        Returns None | 'close' | ('hire', item) | ('unlock', item). Premium outfits
        not in `unlocked` show an Unlock button (one-time fee) instead of Hire."""
        if not self.open:
            return None
        unlocked = unlocked or set()
        rows = self._current()
        n = len(rows)
        x, y, w, h = self._panel_rect()
        mouse = pr.get_mouse_position()

        # --- input -------------------------------------------------------
        if pr.is_key_pressed(pr.KEY_RIGHT) or gamepad.pressed(gamepad.R1):
            self._set_tab(self.tab + 1); rows = self._current(); n = len(rows)
        if pr.is_key_pressed(pr.KEY_LEFT) or gamepad.pressed(gamepad.L1):
            self._set_tab(self.tab - 1); rows = self._current(); n = len(rows)
        if pr.is_key_pressed(pr.KEY_DOWN) or gamepad.pressed(gamepad.DPAD_DOWN):
            self.sel = (self.sel + 1) % n
        if pr.is_key_pressed(pr.KEY_UP) or gamepad.pressed(gamepad.DPAD_UP):
            self.sel = (self.sel - 1) % n
        wheel = pr.get_mouse_wheel_move()
        if wheel:
            self.top = max(0, min(self.top - int(wheel), max(0, n - VISIBLE)))
        if pr.is_key_pressed(pr.KEY_ESCAPE) or gamepad.pressed(gamepad.CIRCLE):
            return "close"
        self._clamp_scroll(n)

        # --- frame -------------------------------------------------------
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pr.draw_rectangle(0, 0, sw, sh, pr.Color(0, 0, 0, 130))
        pr.draw_rectangle(x, y, w, h, BG)
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 2, BORDER)
        pr.draw_rectangle(x, y, w, 44, BAR)
        pr.draw_text("Agent Marketplace", x + PAD, y + 12, 22, pr.RAYWHITE)
        cash_txt = f"Cash  ${cash:,}"
        ctw = pr.measure_text(cash_txt, 20)
        pr.draw_text(cash_txt, x + w - PAD - ctw, y + 13, 20, pr.GOLD)

        # --- tab bar -----------------------------------------------------
        tx, ty = x + PAD, y + 50
        for i, cat in enumerate(self.tabs):
            label = _TAB_LABELS.get(cat, cat.title())
            tw = pr.measure_text(label, 17)
            rect = pr.Rectangle(tx, ty, tw + 22, 26)
            active = i == self.tab
            pr.draw_rectangle_rec(rect, pr.Color(60, 130, 210, 255) if active else ROW)
            pr.draw_text(label, int(tx + 11), int(ty + 5), 17,
                         pr.RAYWHITE if active else pr.Color(170, 180, 200, 255))
            if pr.check_collision_point_rec(mouse, rect) and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
                self._set_tab(i); rows = self._current(); n = len(rows); self._clamp_scroll(n)
            tx += tw + 30

        # --- rows (windowed) ---------------------------------------------
        action = None            # ('hire'|'unlock', item) once committed
        ry = y + 88
        window = rows[self.top:self.top + VISIBLE]
        for off, it in enumerate(window):
            i = self.top + off
            rect = pr.Rectangle(x + PAD, ry, w - 2 * PAD, ROW_H - 5)
            if pr.check_collision_point_rec(mouse, rect):
                self.sel = i
            locked = not is_unlocked(it, unlocked)
            # Locked rows charge the one-time unlock fee; usable rows the hire cost.
            cost = it["unlock"] if locked else it["price"]
            affordable = cash >= cost
            pr.draw_rectangle_rec(rect, ROW_SEL if i == self.sel else ROW)
            name_x = int(rect.x + 12)
            if locked:
                _draw_lock(name_x, int(rect.y + 11), LOCKED)
                name_x += 20
            pr.draw_text(it["name"], name_x, int(rect.y + 9), 19,
                         LOCKED if locked else pr.RAYWHITE)
            price = f"${cost:,}"
            ptw = pr.measure_text(price, 18)
            pr.draw_text(price, int(rect.x + rect.width - 104 - ptw), int(rect.y + 10), 18,
                         AFFORD if affordable else TOO_DEAR)
            bb = pr.Rectangle(rect.x + rect.width - 92, rect.y + 5, 84, ROW_H - 15)
            bhov = pr.check_collision_point_rec(mouse, bb)
            if locked:
                base = UNLOCK_BTN if affordable else pr.Color(70, 74, 84, 255)
                if affordable and bhov:
                    base = LOCKED
                label = "Unlock"
            else:
                base = pr.Color(45, 140, 80, 255) if affordable else pr.Color(70, 74, 84, 255)
                if affordable and bhov:
                    base = pr.Color(60, 175, 100, 255)
                label = "Hire"
            pr.draw_rectangle_rec(bb, base)
            bw = pr.measure_text(label, 18)
            pr.draw_text(label, int(bb.x + (bb.width - bw) / 2), int(bb.y + 5), 18, pr.RAYWHITE)
            if affordable and bhov and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
                action = ("unlock" if locked else "hire", it)
            ry += ROW_H

        # scrollbar hint
        if n > VISIBLE:
            frac = f"{self.sel + 1}/{n}"
            fw = pr.measure_text(frac, 12)
            pr.draw_text(frac, x + w - PAD - fw, y + 64, 12, pr.Color(150, 170, 200, 255))

        if (pr.is_key_pressed(pr.KEY_ENTER) or gamepad.pressed(gamepad.CROSS)) and rows:
            it = rows[self.sel % n]
            locked = not is_unlocked(it, unlocked)
            cost = it["unlock"] if locked else it["price"]
            if cash >= cost:
                action = ("unlock" if locked else "hire", it)
            else:
                self.flash = "Not enough cash"

        foot = self.flash or "L/R tabs  -  Up/Down + wheel scroll  -  Enter / X hire  -  Esc / O close"
        pr.draw_text(foot, x + PAD, y + h - 24, 13, TOO_DEAR if self.flash else pr.LIGHTGRAY)

        if action is not None:
            self.flash = ""
            return action
        return None

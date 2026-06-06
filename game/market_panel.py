"""The brokerage / bank terminal — the idle-market UI.

Looks like a stripped-down trading app: portfolio value up top, a news ticker, a
row per asset with live price + change% + your position and Buy/Sell buttons. The
BANK venue also shows a Savings account (steady interest); the BROKER venue shows
the volatile fictional stocks + crypto.

draw(market, cash) renders the modal and RETURNS an action tuple for the Game to
apply to its cash (single source of truth for money):
  ("buy", asset_id, dollars) | ("sell", asset_id, dollars) | ("sellall", asset_id)
  ("deposit", dollars) | ("withdraw", dollars) | ("withdrawall", None)
or None. Esc closes. A "while you were away" payout card shows first if present.
"""
from __future__ import annotations

import pyray as pr

from . import market as market_mod

_DIM = pr.Color(0, 0, 0, 180)
_BOX = pr.Color(20, 24, 36, 252)
_BAR = pr.Color(30, 36, 52, 255)
_GOLD = pr.Color(232, 196, 96, 255)
_GREEN = pr.Color(80, 210, 130, 255)
_RED = pr.Color(225, 110, 110, 255)
_ACCENT = pr.Color(90, 170, 230, 255)
_INK = pr.RAYWHITE
_DIMTX = pr.Color(135, 145, 165, 255)
_BTN = pr.Color(46, 54, 74, 255)
_BTN_HOT = pr.Color(64, 78, 108, 255)
_BTN_OFF = pr.Color(34, 38, 50, 255)

_TITLES = {"bank": "First City Bank", "broker": "Apex Securities — Trading Floor"}
_TIER_TAG = {"bond": "BONDS", "index": "INDEX", "stock": "STOCK", "crypto": "CRYPTO"}


class MarketPanel:
    def __init__(self) -> None:
        self.open = False
        self.venue = "bank"

    @property
    def capturing(self) -> bool:
        return self.open

    def open_panel(self, venue: str) -> None:
        self.venue = venue
        self.open = True
        while pr.get_char_pressed() > 0:
            pass

    def close(self) -> None:
        self.open = False

    # -- small helpers ------------------------------------------------------
    def _button(self, x, y, w, h, label, enabled=True) -> bool:
        rect = pr.Rectangle(x, y, w, h)
        mouse = pr.get_mouse_position()
        hot = enabled and pr.check_collision_point_rec(mouse, rect)
        col = _BTN_HOT if hot else (_BTN if enabled else _BTN_OFF)
        pr.draw_rectangle_rec(rect, col)
        tw = pr.measure_text(label, 15)
        pr.draw_text(label, int(x + (w - tw) / 2), int(y + (h - 15) / 2), 15,
                     _INK if enabled else _DIMTX)
        return hot and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)

    @staticmethod
    def _money(v: float) -> str:
        return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"

    # -- main draw ----------------------------------------------------------
    def draw(self, market: "market_mod.Market", cash: float):
        if not self.open:
            return None
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pr.draw_rectangle(0, 0, sw, sh, _DIM)
        w, h = 760, 540
        x, y = (sw - w) // 2, (sh - h) // 2
        pr.draw_rectangle(x, y, w, h, _BOX)
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 2, _ACCENT)

        # "while you were away" card takes over until dismissed
        if market.away:
            return self._draw_away(market, x, y, w, h)

        # header: venue + cash + portfolio value
        pr.draw_rectangle(x, y, w, 52, _BAR)
        pr.draw_text(_TITLES.get(self.venue, "Market"), x + 18, y + 14, 24, _INK)
        nw = market.net_worth()
        port = f"Portfolio  {self._money(nw)}"
        pr.draw_text(port, x + w - pr.measure_text(port, 20) - 18, y + 8, 20, _GOLD)
        cashs = f"Cash  {self._money(cash)}"
        pr.draw_text(cashs, x + w - pr.measure_text(cashs, 16) - 18, y + 30, 16, _DIMTX)

        # news ticker
        pr.draw_rectangle(x, y + 52, w, 24, pr.Color(14, 16, 24, 255))
        pr.draw_text(market.news, x + 18, y + 56, 15, _ACCENT)

        action = None
        cy = y + 92

        # bank: a Savings account row first
        if self.venue == "bank":
            action = self._savings_row(market, cash, x + 16, cy, w - 32) or action
            cy += 78

        # one row per asset in this venue
        for a in market.venue_assets(self.venue):
            act = self._asset_row(market, cash, a, x + 16, cy, w - 32)
            action = act or action
            cy += 70

        pr.draw_text("Buy/Sell trade $1,000 at a time  ·  Esc to leave",
                     x + 18, y + h - 28, 15, _DIMTX)
        if pr.is_key_pressed(pr.KEY_ESCAPE):
            self.close()
        return action

    def _savings_row(self, market, cash, x, y, w):
        pr.draw_rectangle(x, y, w, 68, pr.Color(26, 32, 46, 255))
        pr.draw_text("Savings Account", x + 14, y + 10, 19, _INK)
        pr.draw_text("Steady ~10%/mo interest — never goes down.", x + 14, y + 36, 14, _DIMTX)
        bal = f"{self._money(market.savings)}"
        pr.draw_text(bal, x + 320, y + 18, 22, _GREEN)
        bw, bh, gap = 92, 28, 8
        bx = x + w - (bw * 3 + gap * 2) - 14
        by = y + 20
        action = None
        if self._button(bx, by, bw, bh, "Deposit $1k", enabled=cash >= 1000):
            action = ("deposit", market_mod.BUY_CHUNK)
        if self._button(bx + (bw + gap), by, bw, bh, "Withdraw $1k",
                        enabled=market.savings >= 1):
            action = ("withdraw", market_mod.BUY_CHUNK)
        if self._button(bx + (bw + gap) * 2, by, bw, bh, "Withdraw all",
                        enabled=market.savings >= 1):
            action = ("withdrawall", None)
        return action

    def _asset_row(self, market, cash, a, x, y, w):
        pr.draw_rectangle(x, y, w, 60, pr.Color(26, 30, 42, 255))
        pr.draw_rectangle(x, y, 6, 60, _ACCENT)
        pr.draw_text(a.name, x + 16, y + 8, 18, _INK)
        tag = _TIER_TAG.get(a.tier, a.tier.upper())
        pr.draw_text(tag, x + 16 + pr.measure_text(a.name, 18) + 12, y + 11, 13, _DIMTX)
        pr.draw_text(a.blurb, x + 16, y + 34, 13, _DIMTX)
        # price + change%
        price = self._money(a.price)
        pr.draw_text(price, x + 300, y + 8, 19, _INK)
        ch = a.change_pct
        chs = f"{ch:+.1f}%"
        pr.draw_text(chs, x + 300, y + 34, 15, _GREEN if ch >= 0 else _RED)
        # your position
        shares = market.holdings.get(a.id, 0.0)
        val = shares * a.price
        if val >= 1:
            pos = f"You: {self._money(val)}"
            pr.draw_text(pos, x + 420, y + 20, 15, _GOLD)
        # buttons
        bw, bh, gap = 78, 26, 8
        bx = x + w - (bw * 3 + gap * 2) - 12
        by = y + 17
        action = None
        if self._button(bx, by, bw, bh, "Buy $1k", enabled=cash >= 1000):
            action = ("buy", a.id, market_mod.BUY_CHUNK)
        if self._button(bx + (bw + gap), by, bw, bh, "Sell $1k", enabled=val >= 1):
            action = ("sell", a.id, market_mod.BUY_CHUNK)
        if self._button(bx + (bw + gap) * 2, by, bw, bh, "Sell all", enabled=val >= 1):
            action = ("sellall", a.id)
        return action

    def _draw_away(self, market, x, y, w, h):
        info = market.away
        pr.draw_text("While you were away", x + 24, y + 40, 30, _GOLD)
        gained = info.get("gained", 0.0)
        col = _GREEN if gained >= 0 else _RED
        line = (f"Your portfolio {'earned' if gained >= 0 else 'lost'} "
                f"{self._money(abs(gained))}")
        pr.draw_text(line, x + 24, y + 96, 24, col)
        sub = f"over {info.get('hours', 0):.1f} hours · now worth {self._money(info.get('value', 0))}"
        pr.draw_text(sub, x + 24, y + 134, 18, _DIMTX)
        pr.draw_text("Money never sleeps. Reinvest it and grow the empire.",
                     x + 24, y + 180, 16, _ACCENT)
        bw, bh = 200, 40
        if (self._button(x + 24, y + h - 70, bw, bh, "Collect & continue")
                or pr.is_key_pressed(pr.KEY_ENTER) or pr.is_key_pressed(pr.KEY_ESCAPE)):
            market.away = None
        return None

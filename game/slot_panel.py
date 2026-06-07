"""Lucky's Casino — a 3-reel slot machine.

A self-contained modal, opened inside the casino after the one-time risk-tips talk
(same enter-building pattern as the bank/broker market terminal). You pick a bet,
hit Spin, the reels stagger to a stop, and a payout is computed.

draw(cash) renders the modal and RETURNS an action tuple for the Game to apply to
its cash (the single source of truth for money):
  ("cash", -bet)     when a spin starts (the wager is taken)
  ("cash", +payout)  when the reels resolve to a win/push
or None on every other frame. Esc closes (only when the reels are at rest).

Paytable (on a bet B):  777 → 20×B · $$$ → 8×B · any other triple → 4×B ·
any pair → 1×B (your bet back).  No match → you lose the bet. (~20% house edge —
the casino isn't a money printer, which is rather the point of the risk-tips talk.)
"""
from __future__ import annotations

import random

import pyray as pr

_DIM = pr.Color(0, 0, 0, 185)
_BOX = pr.Color(24, 18, 30, 252)
_BAR = pr.Color(42, 26, 44, 255)
_FELT = pr.Color(22, 60, 42, 255)        # casino-green reel window
_GOLD = pr.Color(235, 198, 96, 255)
_GREEN = pr.Color(90, 215, 130, 255)
_RED = pr.Color(228, 105, 105, 255)
_ACCENT = pr.Color(210, 90, 120, 255)    # casino magenta-red trim
_INK = pr.RAYWHITE
_DIMTX = pr.Color(150, 140, 155, 255)
_BTN = pr.Color(150, 50, 70, 255)
_BTN_HOT = pr.Color(190, 70, 92, 255)
_BTN_OFF = pr.Color(54, 40, 50, 255)

# Reel symbols: (glyph, color). Index 0 = "7" (jackpot), 1 = "$".
SYMBOLS = [
    ("7", _GOLD),
    ("$", _GREEN),
    ("C", _RED),                          # cherry
    ("L", pr.Color(230, 215, 90, 255)),   # lemon
    ("B", pr.Color(120, 180, 235, 255)),  # bell
]
BETS = [50, 100, 250, 500]

_REEL_STOP = 0.7        # first reel locks this many seconds after the spin starts
_REEL_STAGGER = 0.4     # each later reel locks this much after the previous one


class SlotPanel:
    def __init__(self) -> None:
        self.open = False
        self._bet_idx = 1                 # default $100
        self._reels = [0, 1, 2]           # resting symbols (indices into SYMBOLS)
        self._final = [0, 1, 2]
        self.spinning = False
        self._spin_start = 0.0
        self._resolved = True
        self._msg = "Pick a bet and pull the handle."
        self._msg_color = _DIMTX
        self._rng = random.Random()

    @property
    def capturing(self) -> bool:
        return self.open

    @property
    def bet(self) -> int:
        return BETS[self._bet_idx]

    def open_panel(self) -> None:
        self.open = True
        self.spinning = False
        self._resolved = True
        self._msg = "Pick a bet and pull the handle."
        self._msg_color = _DIMTX
        while pr.get_char_pressed() > 0:
            pass

    def close(self) -> None:
        self.open = False

    # -- payout logic -------------------------------------------------------
    def _payout(self, reels, bet: int) -> int:
        a, b, c = reels
        if a == b == c:
            if a == 0:
                return bet * 20           # triple 7 — jackpot
            if a == 1:
                return bet * 8            # triple $
            return bet * 4               # any other triple
        if a == b or b == c or a == c:
            return bet                    # any pair — your bet back (a push)
        return 0                          # no match — lose the bet

    def _stop_time(self, i: int) -> float:
        return self._spin_start + _REEL_STOP + i * _REEL_STAGGER

    # -- button helper ------------------------------------------------------
    def _button(self, x, y, w, h, label, enabled=True, size=18) -> bool:
        rect = pr.Rectangle(x, y, w, h)
        hot = enabled and pr.check_collision_point_rec(pr.get_mouse_position(), rect)
        col = _BTN_HOT if hot else (_BTN if enabled else _BTN_OFF)
        pr.draw_rectangle_rec(rect, col)
        pr.draw_rectangle_lines_ex(rect, 2, _GOLD if enabled else _BTN_OFF)
        tw = pr.measure_text(label, size)
        pr.draw_text(label, int(x + (w - tw) / 2), int(y + (h - size) / 2), size,
                     _INK if enabled else _DIMTX)
        return hot and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)

    # -- main draw ----------------------------------------------------------
    def draw(self, cash: float):
        if not self.open:
            return None
        t = pr.get_time()
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pr.draw_rectangle(0, 0, sw, sh, _DIM)
        w, h = 560, 430
        x, y = (sw - w) // 2, (sh - h) // 2
        pr.draw_rectangle(x, y, w, h, _BOX)
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 3, _ACCENT)

        # header
        pr.draw_rectangle(x, y, w, 50, _BAR)
        pr.draw_text("Lucky's Casino — Slots", x + 18, y + 13, 24, _GOLD)
        cashs = f"Cash  ${int(cash):,}"
        pr.draw_text(cashs, x + w - pr.measure_text(cashs, 18) - 18, y + 16, 18, _INK)

        action = None

        # advance / resolve the spin
        if self.spinning and not self._resolved and t >= self._stop_time(2):
            self._resolved = True
            self.spinning = False
            self._reels = list(self._final)
            payout = self._payout(self._reels, self.bet)
            net = payout - self.bet
            if payout >= self.bet * 4:
                self._msg = "JACKPOT!" if self._reels == [0, 0, 0] else f"Big win!  +${net:,}"
                self._msg_color = _GOLD
            elif net > 0:
                self._msg = f"You win!  +${net:,}"
                self._msg_color = _GREEN
            elif payout == self.bet:
                self._msg = "Pair — your bet back."
                self._msg_color = _DIMTX
            else:
                self._msg = f"No match. You lost ${self.bet:,}."
                self._msg_color = _RED
            if payout > 0:
                action = ("cash", payout)

        # reels
        rw, rh, gap = 120, 150, 18
        total = rw * 3 + gap * 2
        rx0 = x + (w - total) // 2
        ry = y + 78
        for i in range(3):
            rx = rx0 + i * (rw + gap)
            pr.draw_rectangle(rx, ry, rw, rh, _FELT)
            pr.draw_rectangle_lines_ex(pr.Rectangle(rx, ry, rw, rh), 3, _GOLD)
            if self.spinning and t < self._stop_time(i):
                idx = int(t * 22 + i * 5) % len(SYMBOLS)   # blur: cycle fast
            else:
                idx = self._final[i] if (self.spinning or self._resolved) else self._reels[i]
            glyph, gcol = SYMBOLS[idx]
            gw = pr.measure_text(glyph, 96)
            pr.draw_text(glyph, int(rx + (rw - gw) / 2), ry + 22, 96, gcol)
        # payline
        pr.draw_line(rx0 - 6, ry + rh // 2, rx0 + total + 6, ry + rh // 2, _ACCENT)

        # message
        mw = pr.measure_text(self._msg, 22)
        pr.draw_text(self._msg, x + (w - mw) // 2, ry + rh + 16, 22, self._msg_color)

        # bet selector + spin button
        by = y + h - 96
        can_bet = not self.spinning
        if self._button(x + 24, by, 40, 44, "<", enabled=can_bet, size=24):
            self._bet_idx = (self._bet_idx - 1) % len(BETS)
        if self._button(x + 24 + 40 + 150 + 8, by, 40, 44, ">", enabled=can_bet, size=24):
            self._bet_idx = (self._bet_idx + 1) % len(BETS)
        # bet readout box
        bx = x + 24 + 40 + 4
        pr.draw_rectangle(bx, by, 150, 44, pr.Color(14, 12, 18, 255))
        pr.draw_rectangle_lines_ex(pr.Rectangle(bx, by, 150, 44), 1, _GOLD)
        betlbl = f"Bet  ${self.bet:,}"
        pr.draw_text(betlbl, bx + (150 - pr.measure_text(betlbl, 20)) // 2, by + 12, 20, _GOLD)
        # spin
        can_spin = (not self.spinning) and cash >= self.bet
        spin_lbl = "Spinning…" if self.spinning else "SPIN"
        if (self._button(x + w - 24 - 180, by, 180, 44, spin_lbl, enabled=can_spin, size=22)
                or (can_spin and (pr.is_key_pressed(pr.KEY_SPACE)
                                  or pr.is_key_pressed(pr.KEY_ENTER)))):
            action = self._spin()

        # paytable + controls footer
        pr.draw_text("777 = 20x   $$$ = 8x   any triple = 4x   any pair = bet back",
                     x + 24, y + h - 42, 14, _DIMTX)
        pr.draw_text("◄ ► change bet   ·   Space/Enter spin   ·   Esc leave",
                     x + 24, y + h - 24, 14, _DIMTX)

        if not self.spinning and pr.is_key_pressed(pr.KEY_ESCAPE):
            self.close()
        return action

    def _spin(self):
        self.spinning = True
        self._resolved = False
        self._spin_start = pr.get_time()
        self._final = [self._rng.randrange(len(SYMBOLS)) for _ in range(3)]
        self._msg = "Good luck…"
        self._msg_color = _INK
        return ("cash", -self.bet)

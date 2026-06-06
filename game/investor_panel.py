"""The investor-meeting modal — pitch the VC, show your homework, take the check.

Opened by walking up to the venture firm and pressing E. It shows the next funding
round, the things the investor needs to see (ticked off from your company profile),
and the amount. If you've captured everything, you can pitch for the check; if not,
it shows exactly what's still missing. draw() returns ("raise", round) when you land
the round, else None; Esc leaves.
"""
from __future__ import annotations

import pyray as pr

from . import investor

_DIM = pr.Color(0, 0, 0, 175)
_BOX = pr.Color(24, 28, 40, 250)
_GOLD = pr.Color(232, 196, 96, 255)
_GREEN = pr.Color(70, 200, 120, 255)
_RED = pr.Color(210, 110, 110, 255)
_ACCENT = pr.Color(90, 170, 230, 255)
_DIMTX = pr.Color(120, 130, 150, 255)

INVESTOR_NAME = "Dana Voss"      # the partner you pitch
FIRM = "Apex Ventures"


class InvestorPanel:
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

    def draw(self, company: dict, raised: set):
        if not self.open:
            return None
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pr.draw_rectangle(0, 0, sw, sh, _DIM)
        w, h = 640, 470
        x, y = (sw - w) // 2, (sh - h) // 2
        pr.draw_rectangle(x, y, w, h, _BOX)
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 2, _ACCENT)
        # speaker tab
        pr.draw_rectangle(x, y - 34, max(260, pr.measure_text(INVESTOR_NAME, 20) + 28), 34, _ACCENT)
        pr.draw_text(f"{INVESTOR_NAME}  -  {FIRM}", x + 16, y - 27, 18, pr.Color(10, 22, 32, 255))

        rnd = investor.next_round(raised)
        action = None
        if rnd is None:
            self._wrap("You've raised every round I've got. Go build something huge.",
                       x + 24, y + 28, w - 48, 22)
            pr.draw_text("Esc to leave", x + 24, y + h - 30, 16, _DIMTX)
            if pr.is_key_pressed(pr.KEY_ESCAPE):
                self.close()
            return None

        self._wrap(rnd.line, x + 24, y + 24, w - 48, 22)
        pr.draw_text(f"{rnd.name.upper()} ROUND", x + 24, y + 92, 18, _ACCENT)
        amt = f"${rnd.amount:,}"
        aw = pr.measure_text(amt, 30)
        pr.draw_text(amt, x + w - 24 - aw, y + 84, 30, _GOLD)

        # checklist of what they want to see
        miss = set(investor.missing(company, rnd.needs))
        cy = y + 132
        for i, key in enumerate(rnd.needs):
            col = i % 2                         # two columns, fill down the rows
            row = i // 2
            ix = x + 24 + col * (w - 48) // 2
            iy = cy + row * 28
            ok = key not in miss
            box = pr.Rectangle(ix, iy + 2, 15, 15)
            if ok:
                pr.draw_rectangle_rec(box, _GREEN)
                pr.draw_text("x", int(box.x) + 3, iy, 15, pr.Color(10, 28, 16, 255))
            else:
                pr.draw_rectangle_lines_ex(box, 2, _RED)
            pr.draw_text(investor.LABELS.get(key, key), ix + 24, iy, 16,
                         pr.RAYWHITE if ok else _DIMTX)

        ready = not miss
        btn = pr.Rectangle(x + 24, y + h - 70, w - 48, 46)
        mouse = pr.get_mouse_position()
        hot = pr.check_collision_point_rec(mouse, btn)
        if ready:
            pr.draw_rectangle_rec(btn, _GREEN if hot else pr.Color(52, 150, 92, 255))
            label = f"Pitch for {amt}"
            lw = pr.measure_text(label, 22)
            pr.draw_text(label, int(btn.x + (btn.width - lw) / 2), int(btn.y + 12), 22, pr.RAYWHITE)
            if (hot and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)) or pr.is_key_pressed(pr.KEY_ENTER):
                action = ("raise", rnd)
                self.close()
        else:
            pr.draw_rectangle_rec(btn, pr.Color(48, 54, 70, 255))
            need = ", ".join(investor.LABELS.get(k, k) for k in rnd.needs if k in miss)
            msg = "Come back when you've nailed down: " + need
            self._wrap(msg, int(btn.x) + 14, int(btn.y) + 8, int(btn.width) - 28, 15, _RED)

        pr.draw_text("Enter to pitch   ·   Esc to leave", x + 24, y + h - 18, 14, _DIMTX)
        if pr.is_key_pressed(pr.KEY_ESCAPE):
            self.close()
        return action

    def _wrap(self, text, x, y, max_w, font, color=None):
        color = color or pr.RAYWHITE
        line, ly = "", y
        for word in text.split(" "):
            trial = (line + " " + word).strip()
            if pr.measure_text(trial, font) > max_w and line:
                pr.draw_text(line, x, ly, font, color)
                line, ly = word, ly + font + 5
            else:
                line = trial
        if line:
            pr.draw_text(line, x, ly, font, color)

"""The Grants Office terminal — apply for an LLM-judged business grant.

Three states:
  * INPUT     — type your case for funding (a real text field).
  * REVIEWING — the application is off with the LLM review board; show a spinner.
  * RESULT    — the verdict: APPROVED (amount + program) or DECLINED, with feedback.

The panel only owns its own UI/text. The Game drives the async judging:
  - draw() returns ("submit", text) once when you submit; the Game then calls
    link.request_grant(...) and panel.set_reviewing().
  - while reviewing, the Game polls link.poll_grant(); on a verdict it pays any
    award and calls panel.set_result(verdict).
Esc closes (a verdict already paid out stays paid).
"""
from __future__ import annotations

import pyray as pr

_DIM = pr.Color(0, 0, 0, 180)
_BOX = pr.Color(22, 26, 38, 252)
_BAR = pr.Color(32, 40, 56, 255)
_GOLD = pr.Color(232, 196, 96, 255)
_GREEN = pr.Color(80, 210, 130, 255)
_RED = pr.Color(225, 110, 110, 255)
_ACCENT = pr.Color(120, 180, 235, 255)
_INK = pr.RAYWHITE
_DIMTX = pr.Color(135, 145, 165, 255)
_FIELD = pr.Color(14, 16, 24, 255)

OFFICER = "Grants Officer"
MAX_LEN = 220

INPUT, REVIEWING, RESULT = "input", "reviewing", "result"


class GrantPanel:
    def __init__(self) -> None:
        self.open = False
        self.state = INPUT
        self.buf = ""
        self.result: dict | None = None

    @property
    def capturing(self) -> bool:
        # only the INPUT field eats keystrokes; reviewing/result don't
        return self.open and self.state == INPUT

    def open_panel(self) -> None:
        self.open = True
        self.state = INPUT
        self.buf = ""
        self.result = None
        while pr.get_char_pressed() > 0:
            pass

    def close(self) -> None:
        self.open = False

    def set_reviewing(self) -> None:
        self.state = REVIEWING

    def set_result(self, verdict: dict) -> None:
        self.result = verdict
        self.state = RESULT

    # -- draw ---------------------------------------------------------------
    def draw(self):
        if not self.open:
            return None
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pr.draw_rectangle(0, 0, sw, sh, _DIM)
        w, h = 660, 380
        x, y = (sw - w) // 2, (sh - h) // 2
        pr.draw_rectangle(x, y, w, h, _BOX)
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 2, _ACCENT)
        pr.draw_rectangle(x, y - 34, max(240, pr.measure_text(OFFICER, 20) + 28), 34, _ACCENT)
        pr.draw_text(f"{OFFICER}  -  Small Business Grants", x + 16, y - 27, 18,
                     pr.Color(10, 22, 32, 255))

        if self.state == INPUT:
            return self._draw_input(x, y, w, h)
        if self.state == REVIEWING:
            return self._draw_reviewing(x, y, w, h)
        return self._draw_result(x, y, w, h)

    def _draw_input(self, x, y, w, h):
        self._wrap("Tell the board why your business deserves a grant — what you'll "
                   "build, who it helps, and what you'd do with the money. Make it count.",
                   x + 24, y + 24, w - 48, 22)
        fld = pr.Rectangle(x + 24, y + 110, w - 48, 150)
        pr.draw_rectangle_rec(fld, _FIELD)
        pr.draw_rectangle_lines_ex(fld, 1, _ACCENT)
        # capture text
        ch = pr.get_char_pressed()
        while ch > 0:
            if 32 <= ch < 127 and len(self.buf) < MAX_LEN:
                self.buf += chr(ch)
            ch = pr.get_char_pressed()
        bs = pr.is_key_pressed(pr.KEY_BACKSPACE)
        if hasattr(pr, "is_key_pressed_repeat"):
            bs = bs or pr.is_key_pressed_repeat(pr.KEY_BACKSPACE)
        if bs and self.buf:
            self.buf = self.buf[:-1]
        caret = "_" if (pr.get_time() % 1.0) < 0.5 else ""
        shown = (self.buf + caret) if self.buf else ("type your application" + caret)
        self._wrap(shown, int(fld.x) + 10, int(fld.y) + 10, int(fld.width) - 20, 22,
                   _GOLD if self.buf else _DIMTX)
        pr.draw_text(f"{len(self.buf)}/{MAX_LEN}", int(fld.x + fld.width) - 70,
                     int(fld.y + fld.height) - 22, 14, _DIMTX)
        pr.draw_text("Enter to submit   ·   Esc to cancel", x + 24, y + h - 30, 16, _DIMTX)
        if pr.is_key_pressed(pr.KEY_ESCAPE):
            self.close()
        elif pr.is_key_pressed(pr.KEY_ENTER) and self.buf.strip():
            return ("submit", self.buf.strip())
        return None

    def _draw_reviewing(self, x, y, w, h):
        dots = "." * (1 + int(pr.get_time() * 2) % 3)
        pr.draw_text("The grant board is reviewing your application" + dots,
                     x + 24, y + 70, 22, _ACCENT)
        self._wrap('"' + self.buf + '"', x + 24, y + 120, w - 48, 20, _DIMTX)
        pr.draw_text("Sit tight — this won't take long.", x + 24, y + h - 30, 16, _DIMTX)
        # no Esc here, so an in-flight verdict isn't lost mid-review
        return None

    def _draw_result(self, x, y, w, h):
        r = self.result or {}
        approved = r.get("approved")
        if approved:
            head = f"APPROVED — ${int(r.get('amount', 0)):,}"
            pr.draw_text(head, x + 24, y + 30, 30, _GREEN)
            pr.draw_text(str(r.get("program", "Grant")), x + 24, y + 72, 18, _GOLD)
        else:
            pr.draw_text("DECLINED", x + 24, y + 30, 30, _RED)
            pr.draw_text(str(r.get("program", "Grant")), x + 24, y + 72, 18, _DIMTX)
        self._wrap(str(r.get("feedback", "")), x + 24, y + 110, w - 48, 24, _INK)
        pr.draw_text("Enter / Esc to leave", x + 24, y + h - 30, 16, _DIMTX)
        if pr.is_key_pressed(pr.KEY_ENTER) or pr.is_key_pressed(pr.KEY_ESCAPE):
            self.close()
        return None

    @staticmethod
    def _wrap(text, x, y, max_w, lh, color=_INK):
        cur = ""
        for word in text.split(" "):
            trial = (cur + " " + word).strip()
            if pr.measure_text(trial, 18) > max_w and cur:
                pr.draw_text(cur, x, y, 18, color)
                cur, y = word, y + lh
            else:
                cur = trial
        if cur:
            pr.draw_text(cur, x, y, 18, color)

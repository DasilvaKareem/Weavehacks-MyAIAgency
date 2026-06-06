"""The Company Dossier — view and edit every decision your agents read.

A centered panel that lists the company's identity and full Business Model Canvas
(name, pitch, value proposition, customer, channels, relationships, business model,
pricing, key resources/activities/partners, cost structure, brand, domain, logo,
competitors). Click a row to edit it in place; the typed value is returned to the
game, which saves it to the company profile so the backend briefs every agent with
it (see backend/company.py).

Pure UI: `draw(company)` renders + handles input and returns an action the game
applies — None on a normal frame, ("set", key, value) when a field is saved.
The game owns the data and persistence; this panel never touches the store.
"""
from __future__ import annotations

import pyray as pr

_PANEL = pr.Color(22, 26, 36, 248)
_ACCENT = pr.Color(90, 170, 235, 255)
_HEAD = pr.Color(150, 165, 190, 255)
_SET = pr.GOLD
_UNSET = pr.Color(110, 120, 140, 255)
_ROW = pr.Color(30, 36, 50, 255)
_ROW_HOT = pr.Color(44, 58, 84, 255)

# (profile key, label, edit prompt) — order matches how it reads as a one-pager.
# Must stay in sync with backend/company.py _FIELDS (what the agents actually read).
FIELDS: list[tuple[str, str, str]] = [
    ("name", "Company name", "What's the company called?"),
    ("industry", "Industry", "What industry are you in?"),
    ("pitch", "One-line pitch", "Pitch it in one line"),
    ("value_prop", "Value proposition", "What's your core value proposition?"),
    ("customer", "Target customer", "Who's your customer?"),
    ("channels", "Channels", "How do you reach customers?"),
    ("relationships", "Customer relationships", "How do you keep customers?"),
    ("business_model", "Business model", "How does it make money?"),
    ("pricing", "Pricing", "How do you price it?"),
    ("key_resources", "Key resources", "What key resources do you need?"),
    ("key_activities", "Key activities", "What are your key activities?"),
    ("partnerships", "Key partners", "Who are your key partners?"),
    ("cost_structure", "Cost structure", "What are your biggest costs?"),
    ("brand", "Brand & colors", "Describe your brand & colors"),
    ("domain", "Website / domain", "What domain do you want?"),
    ("logo", "Logo", "Describe the logo you want"),
    ("competitors", "Competitors", "Who are your main competitors?"),
]

_ROW_H = 44
_PAD = 24
_HEADER_H = 84
_W = 660


class DossierPanel:
    def __init__(self) -> None:
        self.open = False
        self._editing: str | None = None      # key of the field being edited
        self._buf = ""
        self._scroll = 0.0

    @property
    def capturing(self) -> bool:
        """True while a text field has focus, so the game freezes movement/keys."""
        return self.open and self._editing is not None

    def toggle(self) -> None:
        self.open = not self.open
        self._editing, self._buf, self._scroll = None, "", 0.0

    def close(self) -> None:
        self.open = False
        self._editing, self._buf = None, ""

    def _rect(self) -> tuple[int, int, int, int]:
        sh = pr.get_screen_height()
        foot = 96 if self._editing else 40
        needed = _HEADER_H + len(FIELDS) * _ROW_H + foot
        h = min(needed, sh - 40)
        return (pr.get_screen_width() - _W) // 2, (sh - h) // 2, _W, h

    def draw(self, company: dict):
        """Render + handle input. Returns None | ("set", key, value)."""
        if not self.open:
            return None
        x, y, w, h = self._rect()
        mouse = pr.get_mouse_position()
        sw, sh = pr.get_screen_width(), pr.get_screen_height()

        pr.draw_rectangle(0, 0, sw, sh, pr.Color(0, 0, 0, 150))
        pr.draw_rectangle(x, y, w, h, _PANEL)
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 2, _ACCENT)
        pr.draw_rectangle(x, y, w, 4, _ACCENT)

        pr.draw_text("COMPANY DOSSIER", x + _PAD, y + 18, 26, pr.RAYWHITE)
        filled = sum(1 for k, _, _ in FIELDS if company.get(k))
        sub = f"{filled} of {len(FIELDS)} decided  ·  your agents read every line"
        pr.draw_text(sub, x + _PAD, y + 52, 15, _HEAD)

        foot_h = 96 if self._editing else 40
        view_top = y + _HEADER_H
        view_h = h - _HEADER_H - foot_h
        editing_active = self._editing is not None

        action = None
        pr.begin_scissor_mode(x, view_top, w, view_h)
        ry = view_top + int(self._scroll)
        for key, label, _prompt in FIELDS:
            if ry + _ROW_H >= view_top and ry <= view_top + view_h:   # visible only
                row = pr.Rectangle(x + 12, ry, w - 24, _ROW_H - 6)
                inside = pr.check_collision_point_rec(mouse, row) and \
                    view_top <= mouse.y <= view_top + view_h
                hot = (not editing_active) and inside
                is_edit = self._editing == key
                pr.draw_rectangle_rec(row, _ROW_HOT if (hot or is_edit) else _ROW)
                pr.draw_text(label.upper(), int(row.x) + 12, int(row.y) + 5, 12, _HEAD)
                value = company.get(key, "")
                shown = value if value else "— not set —  (click to add)"
                shown = shown if len(shown) <= 62 else shown[:59] + "…"
                pr.draw_text(shown, int(row.x) + 12, int(row.y) + 20, 17,
                             _SET if value else _UNSET)
                if hot and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
                    self._editing, self._buf = key, value
            ry += _ROW_H
        pr.end_scissor_mode()

        # scroll (wheel) — clamp so the list can't drift past its ends
        content_h = len(FIELDS) * _ROW_H
        max_scroll = min(0.0, view_h - content_h)
        if not editing_active and pr.check_collision_point_rec(
                mouse, pr.Rectangle(x, view_top, w, view_h)):
            self._scroll += pr.get_mouse_wheel_move() * 36.0
        self._scroll = max(max_scroll, min(0.0, self._scroll))

        if self._editing is not None:
            action = self._draw_editor(x, y, w, h) or action
        else:
            hint = "Click a line to edit   ·   scroll for more   ·   C to close"
            pr.draw_text(hint, x + _PAD, y + h - 28, 15, _UNSET)

        # Esc closes when not mid-edit (C is the open/close toggle, handled by the
        # game so typing a "C" into a field never slams the panel shut).
        if self._editing is None and pr.is_key_pressed(pr.KEY_ESCAPE):
            self.close()
        return action

    def _draw_editor(self, x: int, y: int, w: int, h: int):
        """Bottom text field for the field being edited. Returns ("set", key, value)
        on Enter with text, else None; Esc cancels the edit."""
        key = self._editing
        prompt = next(p for k, _, p in FIELDS if k == key)
        fy = y + h - 84
        pr.draw_text(prompt.upper(), x + _PAD, fy, 14, _ACCENT)
        field = pr.Rectangle(x + _PAD, fy + 20, w - 2 * _PAD, 40)
        pr.draw_rectangle_rec(field, pr.Color(14, 16, 24, 255))
        pr.draw_rectangle_lines_ex(field, 1, _ACCENT)
        ch = pr.get_char_pressed()
        while ch > 0:
            if 32 <= ch < 127 and len(self._buf) < 80:
                self._buf += chr(ch)
            ch = pr.get_char_pressed()
        bs = pr.is_key_pressed(pr.KEY_BACKSPACE)
        if hasattr(pr, "is_key_pressed_repeat"):
            bs = bs or pr.is_key_pressed_repeat(pr.KEY_BACKSPACE)
        if bs and self._buf:
            self._buf = self._buf[:-1]
        caret = "_" if (pr.get_time() % 1.0) < 0.5 else ""
        shown = (self._buf + caret) if self._buf else ("type here" + caret)
        pr.draw_text(shown, int(field.x) + 10, int(field.y) + 10, 20,
                     _SET if self._buf else _UNSET)
        pr.draw_text("Enter to save   ·   Esc to cancel", x + _PAD, y + h - 24, 14, _HEAD)
        if pr.is_key_pressed(pr.KEY_ESCAPE):
            self._editing, self._buf = None, ""
            return None
        if pr.is_key_pressed(pr.KEY_ENTER) and self._buf.strip():
            out = ("set", key, self._buf.strip())
            self._editing, self._buf = None, ""
            return out
        return None

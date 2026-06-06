"""The on-screen to-do list + the always-on current-task banner.

Reads a tasks.TaskBoard and draws it; holds no game state of its own beyond the
open/closed toggle and the scroll offset. Call `draw_objective()` every frame for
the little "next to-do" HUD chip, and toggle the full `TodoList` panel (the
chaptered checklist) with a key.
"""
from __future__ import annotations

import pyray as pr

from . import tasks

_PANEL = pr.Color(22, 26, 36, 244)
_HEAD = pr.Color(150, 165, 190, 255)
_ACCENT = pr.Color(70, 130, 220, 255)
_DONE = pr.Color(70, 200, 120, 255)
_DIM = pr.Color(96, 104, 122, 255)


def draw_objective(board: tasks.TaskBoard, x: int = 18, y: int = 64) -> None:
    """A compact 'next to-do' chip: progress + the active task title."""
    cur = board.current()
    done, total = board.progress()
    label = cur.title if cur else "Company built — you win!"
    w = max(280, pr.measure_text(label, 20) + 150)
    pr.draw_rectangle(x, y, w, 50, pr.Color(20, 24, 34, 220))
    pr.draw_rectangle(x, y, 5, 50, _ACCENT)
    pr.draw_text(f"TO-DO  {done}/{total}", x + 16, y + 7, 13, _HEAD)
    pr.draw_text(label, x + 16, y + 24, 20, pr.RAYWHITE if cur else _DONE)


class TodoList:
    """The full chaptered to-do panel. Toggle `open`, scroll with the wheel.

    The current task is actionable when it's a manual one (no auto hook): click it
    to do it. Text tasks (name, pitch...) open an input field; `draw()` then returns
    a small action tuple the game acts on:
      ("done", task)            -> mark it complete (and pay its reward)
      ("answer", task, value)   -> store the typed value on the profile, then done
    Returns None on any normal frame.
    """

    def __init__(self) -> None:
        self.open = False
        self._scroll = 0.0
        self._asking = None       # the Task whose input field is showing
        self._buf = ""
        # Task keys completed out in the city (quest-stop buildings), so the list
        # doesn't also offer to do them here — you walk up to the building instead.
        self.quest_keys: set[str] = set()

    @property
    def capturing(self) -> bool:
        """True while a text field has focus, so the game freezes movement/keys."""
        return self.open and self._asking is not None

    def toggle(self) -> None:
        self.open = not self.open
        self._scroll = 0.0
        self._asking = None
        self._buf = ""

    def draw(self, board: tasks.TaskBoard):
        if not self.open:
            return None
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pw = 460
        px = sw - pw
        mouse = pr.get_mouse_position()
        pr.draw_rectangle(px, 0, pw, sh, _PANEL)
        pr.draw_rectangle(px, 0, 4, sh, _ACCENT)

        done, total = board.progress()
        pr.draw_text("TO-DO LIST", px + 24, 22, 24, pr.RAYWHITE)
        pr.draw_text(f"{done} of {total} done", px + 24, 52, 16, _HEAD)
        bx, bw = px + 24, pw - 48
        pr.draw_rectangle(bx, 76, bw, 8, pr.Color(40, 46, 62, 255))
        pr.draw_rectangle(bx, 76, int(bw * (done / max(1, total))), 8, _DONE)

        cur = board.current()
        action = None
        bottom = sh - (96 if self._asking else 36)   # leave room for the input bar

        top = 100
        pr.begin_scissor_mode(px, top, pw, bottom - top)
        y = top + int(self._scroll)
        for chapter in tasks.CHAPTERS:
            pr.draw_text(chapter.upper(), px + 24, y, 15, _ACCENT)
            y += 26
            for t in (t for t in tasks.TASKS if t.chapter == chapter):
                is_done = board.is_done(t.key)
                is_cur = cur is not None and t.key == cur.key
                # Clickable here only if it's a typed task that ISN'T a city errand.
                actionable = (is_cur and bool(t.ask)
                              and t.key not in self.quest_keys and self._asking is None)
                row_h = 46 if is_cur else 26
                if actionable:                          # highlight the clickable row
                    hot = pr.check_collision_point_rec(
                        mouse, pr.Rectangle(px + 8, y - 2, pw - 16, row_h))
                    pr.draw_rectangle(px + 8, y - 2, pw - 16, row_h,
                                      pr.Color(40, 60, 96, 255) if hot else pr.Color(30, 40, 60, 255))
                    if hot and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
                        if t.ask:
                            self._asking, self._buf = t, ""
                        else:
                            action = ("done", t)
                box = pr.Rectangle(px + 26, y + 1, 16, 16)
                if is_done:
                    pr.draw_rectangle_rec(box, _DONE)
                    pr.draw_text("x", int(box.x) + 4, y, 16, pr.Color(12, 30, 18, 255))
                else:
                    pr.draw_rectangle_lines_ex(box, 2, _ACCENT if is_cur else _DIM)
                col = _DONE if is_done else (pr.RAYWHITE if is_cur else _DIM)
                pr.draw_text(t.title, px + 52, y, 18, col)
                if is_cur:
                    if actionable:
                        hint = "Click to fill this in"
                    elif t.key in self.quest_keys:
                        hint = "Head into the city and find the right spot"
                    else:
                        hint = t.desc
                    pr.draw_text("> " + hint, px + 52, y + 20, 14, _HEAD)
                    y += 20
                y += 26
            y += 8
        pr.end_scissor_mode()

        content_h = y - (top + int(self._scroll))
        max_scroll = min(0, (bottom - top) - content_h - 20)
        self._scroll += pr.get_mouse_wheel_move() * 36.0
        self._scroll = max(max_scroll, min(0.0, self._scroll))

        if self._asking is not None:
            action = self._draw_input(px, pw, sh) or action
        else:
            pr.draw_text("L to close", px + 24, sh - 30, 16, _DIM)
        return action

    def _draw_input(self, px, pw, sh):
        """The text field at the bottom for an `ask` task. Returns ("answer", ...)
        on Enter with text, else None; Esc cancels."""
        t = self._asking
        fy = sh - 86
        pr.draw_text(t.ask.upper(), px + 24, fy, 14, _ACCENT)
        field = pr.Rectangle(px + 24, fy + 20, pw - 48, 40)
        pr.draw_rectangle_rec(field, pr.Color(14, 16, 24, 255))
        pr.draw_rectangle_lines_ex(field, 1, _ACCENT)
        ch = pr.get_char_pressed()
        while ch > 0:
            if 32 <= ch < 127 and len(self._buf) < 48:
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
                     pr.GOLD if self._buf else pr.Color(110, 120, 140, 255))
        pr.draw_text("Enter to save   ·   Esc to cancel", px + 24, sh - 22, 14, _DIM)
        if pr.is_key_pressed(pr.KEY_ESCAPE):
            self._asking, self._buf = None, ""
            return None
        if pr.is_key_pressed(pr.KEY_ENTER) and self._buf.strip():
            done = ("answer", t, self._buf.strip())
            self._asking, self._buf = None, ""
            return done
        return None

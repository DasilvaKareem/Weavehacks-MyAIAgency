"""The Global AI Terminal — full-screen CRT overlay for the CEO Desk.

A 90s green-phosphor terminal that fills most of the screen: the CEO types a
directive and the company's chief-of-staff AI (backend/ceo_terminal.py) actually
delegates the work to hired employees and streams the result back, token by
token, with the live tool step ("> using delegate_to") showing who it's pulling
in. Same non-blocking model path as the in-office chat — the panel only ever
polls, never blocks the render loop.

Opened by walking up to the CEO Desk and pressing E (see main.py). While open it
owns the keyboard; the game freezes movement so typing doesn't walk the CEO.
"""
from __future__ import annotations

import re

import pyray as pr

from . import gamepad, voice
from .chat_panel import _wrap, _open_externally
from backend.config import GEMINI_MODEL
from backend.ceo_terminal import TERMINAL_ID

# Any http(s) or file:// link in the transcript is drawn underlined and click-to-open
# (file:// links open a real drive file/site on the CEO's own computer).
_URL_RE = re.compile(r'(?:https?|file)://[^\s<>"\')\]]+')
# Trailing punctuation that's almost never part of the URL (sentence/paren noise).
_URL_TRIM = ".,;:!?)]}'\""

# CRT palette — green phosphor on near-black, amber for the operator's own input.
BG = pr.Color(6, 12, 8, 248)
SCREEN = pr.Color(8, 16, 10, 255)
GREEN = pr.Color(120, 255, 150, 255)        # terminal / AI output
GREEN_DIM = pr.Color(70, 150, 90, 255)
GREEN_FAINT = pr.Color(40, 90, 55, 255)
AMBER = pr.Color(255, 205, 110, 255)        # the CEO's typed lines
CYAN = pr.Color(120, 230, 230, 255)         # live activity / tool steps
LINK = pr.Color(110, 200, 255, 255)         # clickable URL
LINK_HOVER = pr.Color(190, 230, 255, 255)
RED = pr.Color(235, 110, 90, 255)
SCANLINE = pr.Color(0, 0, 0, 40)

FONT = 20
LINE_H = 26
PAD = 28
MARGIN = 46                                  # screen edge -> panel (uses lots of screen)
MAX_INPUT = 400
SPINNER = "|/-\\"


class TerminalPanel:
    def __init__(self, link) -> None:
        self.link = link
        self.open = False
        self.input = ""
        self.waiting = False
        self._lines: list[tuple[pr.Color, str]] = []   # cached wrapped transcript
        self._scroll = 0                # lines scrolled up from the bottom (0 = newest)
        self._status = ""               # transient line in the input row (voice, etc.)
        self._step = ""                 # live tool-loop activity ("using X")
        self._partial = ""              # streamed answer so far this turn
        self._wait_start = 0.0
        self.voice = voice.VoiceInput(GEMINI_MODEL)    # push-to-talk (no-op w/o mic)
        self._company = ""              # company name for the prompt, set on open
        # Clickable URL hit-boxes, rebuilt every draw() and consumed by update()
        # next frame (same set-in-draw / use-in-update pattern as the chat panel).
        self._link_rects: list[tuple[pr.Rectangle, str]] = []
        # Hiring: a proposal from the hire_agent tool awaiting the CEO's Y/N, and the
        # game callback on_hire(req)->result that does the budget check + real hire.
        self.on_hire = None
        self._hire = None
        # A verified-live URL from the latest reply, offered for one-press opening.
        self._open_url = None

    # --- lifecycle ---------------------------------------------------------

    def open_panel(self, company_name: str = "") -> None:
        self.open = True
        self.input = ""
        self._company = (company_name or "company").strip().lower().replace(" ", "-")[:18]
        self.waiting = self.link.is_busy(TERMINAL_ID)
        self._wait_start = pr.get_time() if self.waiting else 0.0
        self._step = self._partial = self._status = ""
        self._link_rects = []
        self._hire = None
        self._open_url = None
        while pr.get_char_pressed() > 0:    # drop the 'e' that opened the panel
            pass
        self._refresh()

    def close(self) -> None:
        self.voice.cancel()
        voice.stop_speaking()
        pr.set_mouse_cursor(pr.MOUSE_CURSOR_DEFAULT)    # drop any link-hover pointer
        self.open = False
        self.input = ""
        self.waiting = False
        self._step = self._partial = self._status = ""
        self._link_rects = []
        self._hire = None
        self._open_url = None

    @property
    def capturing(self) -> bool:
        """True while the terminal owns the keyboard (mirrors PhonePanel)."""
        return self.open

    # --- send --------------------------------------------------------------

    def _submit(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self.link.terminal_send(text):
            self.input = ""
            self.waiting = True
            self._step = self._partial = ""
            self._open_url = None
            self._wait_start = pr.get_time()
            self._refresh()

    def _prompt(self) -> str:
        return f"ceo@{self._company}:~$ "

    def _confirm_hire(self, yes: bool) -> None:
        """Resolve a pending hire proposal: on yes the game callback checks the
        budget + actually hires; either way the outcome is posted to the transcript."""
        req = self._hire or {}
        self._hire = None
        self.link.clear_terminal_hire()
        while pr.get_char_pressed() > 0:        # swallow the y/n keystroke
            pass
        if not yes:
            self.link.terminal_append("ai", "Hire cancelled — nobody hired, nothing spent.")
        elif self.on_hire is not None:
            self.link.terminal_append("ai", self.on_hire(req) or "Hire couldn't be completed.")
        else:
            self.link.terminal_append("ai", "Hiring isn't available right now.")
        self._refresh()

    # --- push-to-talk ------------------------------------------------------

    def _update_voice(self) -> None:
        held = gamepad.down(gamepad.R2) or pr.is_key_down(pr.KEY_LEFT_CONTROL)
        if held and not self.voice.recording and not self.voice.transcribing:
            self.voice.begin()
            self._status = "REC  speak now..."
        elif self.voice.recording and not held:
            self.voice.end()
            self._status = "transcribing..." if self.voice.transcribing else ""
        result = self.voice.poll()
        if result is None:
            return
        self._status = ""
        if result.startswith("[voice error"):
            self._status = result
        elif result.strip():
            self._submit(result)

    # --- per-frame ---------------------------------------------------------

    def update(self) -> None:
        if not self.open:
            return

        wheel = pr.get_mouse_wheel_move()
        if wheel:
            self._scroll = max(0, self._scroll + int(wheel * 3))

        # Click a link to open it in the system browser (rects from last draw()).
        if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
            m = pr.get_mouse_position()
            for rect, url in self._link_rects:
                if pr.check_collision_point_rec(m, rect):
                    _open_externally(url)
                    self._status = f"opening {url[:48]}..."
                    return

        # A hire the terminal proposed: take it once the turn that asked is done,
        # then let the CEO confirm (Y) or cancel (N/Esc) right here.
        if self._hire is None and not self.waiting:
            pend = self.link.poll_terminal_hire()
            if pend:
                self._hire = pend
                self._scroll = 0
        if self._hire is not None:
            if pr.is_key_pressed(pr.KEY_Y):
                self._confirm_hire(True)
            elif pr.is_key_pressed(pr.KEY_N) or pr.is_key_pressed(pr.KEY_ESCAPE):
                self._confirm_hire(False)
            return                              # modal: block typing/closing while deciding

        if self.waiting:
            step = self.link.poll_steps(TERMINAL_ID)
            if step:
                self._step = step
            for tok in self.link.poll_tokens(TERMINAL_ID):
                if tok is None:
                    self._partial = ""
                else:
                    self._partial += tok
            reply = self.link.poll_reply(TERMINAL_ID)
            if reply is not None:
                self.waiting = False
                self._step = self._partial = ""
                self._refresh()
                if not reply.startswith("[error"):
                    voice.speak(reply, None)        # the terminal reads it back
                # Offer to open any real URL it returned. The backend already
                # stripped dead links (leaving a "[dead link removed …]" marker), so
                # drop those markers first — only verified-live URLs remain.
                clean = re.sub(r"\[dead link removed[^\]]*\]", "", reply)
                urls = _URL_RE.findall(clean)
                self._open_url = urls[-1].rstrip(_URL_TRIM) if urls else None

        # A real URL came back — offer to open it (only while the input is empty, so
        # an 'o' you type mid-message isn't hijacked). Clicking the link also works.
        if self._open_url is not None and not self.input:
            if pr.is_key_pressed(pr.KEY_O):
                _open_externally(self._open_url)
                self._status = f"opening {self._open_url[:48]}..."
                self._open_url = None
                while pr.get_char_pressed() > 0:    # swallow the 'o'
                    pass
                return
            if pr.is_key_pressed(pr.KEY_ESCAPE):   # Esc dismisses the prompt first
                self._open_url = None
                return

        if pr.is_key_pressed(pr.KEY_ESCAPE) or gamepad.pressed(gamepad.CIRCLE):
            self.close()
            return

        self._update_voice()                        # push-to-talk works even while waiting
        if self.waiting or self.voice.recording or self.voice.transcribing:
            return                                  # ignore typing while busy

        ch = pr.get_char_pressed()
        while ch > 0:
            if 32 <= ch < 127 and len(self.input) < MAX_INPUT:
                self.input += chr(ch)
                self._status = ""
            ch = pr.get_char_pressed()

        backspace = pr.is_key_pressed(pr.KEY_BACKSPACE)
        if hasattr(pr, "is_key_pressed_repeat"):
            backspace = backspace or pr.is_key_pressed_repeat(pr.KEY_BACKSPACE)
        if backspace and self.input:
            self.input = self.input[:-1]

        if pr.is_key_pressed(pr.KEY_ENTER) and self.input.strip():
            self._submit(self.input)

    # --- transcript cache --------------------------------------------------

    def _body_w(self) -> int:
        sw = pr.get_screen_width()
        return sw - 2 * MARGIN - 2 * PAD

    def _refresh(self) -> None:
        body_w = self._body_w()
        lines: list[tuple[pr.Color, str]] = []
        for m in self.link.terminal_history():
            if m.role == "human":
                for i, wl in enumerate(_wrap(m.content, body_w, FONT)):
                    prefix = self._prompt() if i == 0 else "  "
                    lines.append((AMBER, prefix + wl))
            else:
                for wl in _wrap(m.content, body_w, FONT):
                    lines.append((GREEN, wl))
            lines.append((BG, ""))          # blank spacer between turns
        self._lines = lines
        self._scroll = 0

    def _wait_line(self) -> str:
        spin = SPINNER[int(pr.get_time() * 8) % len(SPINNER)]
        elapsed = max(0, int(pr.get_time() - self._wait_start)) if self._wait_start else 0
        verb = self._step or "working the company"
        clock = f"  [{elapsed}s]" if elapsed else ""
        return f"{spin} {verb}...{clock}"

    # --- draw --------------------------------------------------------------

    def draw(self) -> None:
        if not self.open:
            return
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pr.draw_rectangle(0, 0, sw, sh, pr.Color(0, 0, 0, 235))

        x, y = MARGIN, MARGIN
        w, h = sw - 2 * MARGIN, sh - 2 * MARGIN
        pr.draw_rectangle(x, y, w, h, SCREEN)
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 2, GREEN_DIM)

        # --- title bar ---
        blink = "_" if (pr.get_time() % 1.0) < 0.5 else " "
        title = "COMPANY.AI  //  GLOBAL AI TERMINAL" + blink
        pr.draw_text(title, x + PAD, y + 16, 26, GREEN)
        status = "[ ONLINE ]" if not self.waiting else "[ BUSY  ]"
        sc = CYAN if not self.waiting else AMBER
        sw_t = pr.measure_text(status, 18)
        pr.draw_text(status, x + w - PAD - sw_t, y + 20, 18, sc)
        sub = "delegates your orders to the team and gets it done"
        pr.draw_text(sub, x + PAD, y + 46, 15, GREEN_DIM)
        pr.draw_line(x + PAD, y + 70, x + w - PAD, y + 70, GREEN_FAINT)

        # --- transcript body ---
        body_top = y + 84
        input_y = y + h - 78
        visible = max(1, (input_y - body_top) // LINE_H)
        max_scroll = max(0, len(self._lines) - visible)
        if self._scroll > max_scroll:
            self._scroll = max_scroll
        end = len(self._lines) - self._scroll
        window = self._lines[max(0, end - visible):end]
        ty = body_top
        self._link_rects = []                       # rebuilt below, used next frame
        hovering_link = False
        for color, line in window:
            if line:
                hovering_link |= self._draw_body_line(line, color, x + PAD, ty)
            ty += LINE_H
        # Pointer cursor when hovering a link, default otherwise.
        pr.set_mouse_cursor(pr.MOUSE_CURSOR_POINTING_HAND if hovering_link
                            else pr.MOUSE_CURSOR_DEFAULT)

        if len(self._lines) <= 1:           # empty terminal: a friendly hint
            tip = "Type an order and press ENTER, e.g.  build a landing page and write a launch post"
            pr.draw_text(tip, x + PAD, body_top, FONT, GREEN_FAINT)

        # While waiting (and pinned to newest): stream the answer, else the step line.
        if self.waiting and self._scroll == 0:
            if self._partial:
                cursor = "_" if int(pr.get_time() * 2) % 2 == 0 else ""
                for wl in _wrap(self._partial + cursor, self._body_w(), FONT):
                    if ty >= input_y - LINE_H:
                        break
                    pr.draw_text(wl, x + PAD, ty, FONT, GREEN)
                    ty += LINE_H
            elif ty < input_y - LINE_H:
                pr.draw_text(self._wait_line(), x + PAD, ty, FONT, CYAN)
                ty += LINE_H

        if self._scroll > 0:
            tag = "^ scroll down for latest"
            tw = pr.measure_text(tag, 13)
            pr.draw_text(tag, x + w - PAD - tw, body_top - 16, 13, GREEN_DIM)

        # --- input line ---
        pr.draw_line(x + PAD, input_y - 8, x + w - PAD, input_y - 8, GREEN_FAINT)
        if self._hire is not None:
            role = self._hire.get("role") or "?"
            pr.draw_rectangle(x + PAD - 6, input_y - 4, w - 2 * PAD + 12, 30,
                              pr.Color(50, 38, 10, 255))
            bar = f"HIRE REQUEST -> {role}    [Y] confirm (checks your budget)    [N] cancel"
            pr.draw_text(bar, x + PAD, input_y, FONT, AMBER)
        elif self._open_url is not None and not self.input:
            shown = self._open_url if len(self._open_url) <= 64 else self._open_url[:61] + "..."
            pr.draw_rectangle(x + PAD - 6, input_y - 4, w - 2 * PAD + 12, 30,
                              pr.Color(10, 40, 28, 255))
            pr.draw_text(f"OPEN -> {shown}    [O] open in browser    [Esc] dismiss",
                         x + PAD, input_y, FONT, CYAN)
        elif self._status:
            col = RED if self._status.startswith("[voice error") else AMBER
            pr.draw_text(self._status, x + PAD, input_y, FONT, col)
        elif self.waiting:
            pr.draw_text("...working — you can wait or press ESC to step away",
                         x + PAD, input_y, FONT, GREEN_DIM)
        else:
            caret = "█" if (pr.get_time() % 1.0) < 0.5 else " "
            prompt = self._prompt()
            pr.draw_text(prompt, x + PAD, input_y, FONT, GREEN_DIM)
            pw = pr.measure_text(prompt, FONT)
            pr.draw_text(self.input + caret, x + PAD + pw, input_y, FONT, AMBER)

        talk = "hold Ctrl talk" if voice.available() else "(mic off)"
        hint = f"ENTER send   ·   {talk}   ·   scroll: history   ·   ESC close"
        pr.draw_text(hint, x + PAD, y + h - 26, 14, GREEN_DIM)

        self._draw_scanlines(x, y, w, h)

    def _draw_body_line(self, line: str, color, x: int, ty: int) -> bool:
        """Draw one transcript line; render any URL underlined + clickable. Records
        hit-boxes into self._link_rects and returns True if the mouse is over a link."""
        matches = list(_URL_RE.finditer(line))
        if not matches:
            pr.draw_text(line, x, ty, FONT, color)
            return False
        m = pr.get_mouse_position()
        cursor, idx, hovering = x, 0, False
        for mo in matches:
            pre = line[idx:mo.start()]
            if pre:
                pr.draw_text(pre, cursor, ty, FONT, color)
                cursor += pr.measure_text(pre, FONT)
            full = mo.group(0)
            url = full.rstrip(_URL_TRIM) or full      # don't grab trailing punctuation
            uw = pr.measure_text(url, FONT)
            rect = pr.Rectangle(cursor, ty - 1, uw, FONT + 4)
            hot = pr.check_collision_point_rec(m, rect)
            hovering = hovering or hot
            ucol = LINK_HOVER if hot else LINK
            pr.draw_text(url, cursor, ty, FONT, ucol)
            pr.draw_line(int(cursor), ty + FONT, int(cursor + uw), ty + FONT, ucol)
            self._link_rects.append((rect, url))
            cursor += uw
            tail = full[len(url):]                     # the stripped punctuation, plain
            if tail:
                pr.draw_text(tail, cursor, ty, FONT, color)
                cursor += pr.measure_text(tail, FONT)
            idx = mo.end()
        rest = line[idx:]
        if rest:
            pr.draw_text(rest, cursor, ty, FONT, color)
        return hovering

    def _draw_scanlines(self, x: int, y: int, w: int, h: int) -> None:
        """Cheap CRT feel: faint horizontal lines across the screen area."""
        for ly in range(y + 2, y + h - 2, 3):
            pr.draw_line(x + 2, ly, x + w - 2, ly, SCANLINE)

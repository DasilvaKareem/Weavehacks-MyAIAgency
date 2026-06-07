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
        # Files browser (Tab toggles it): the drive's files/assets, with preview.
        self.screen = "chat"            # "chat" | "files"
        self._files: list = []          # cached FileRow list
        self._fsel = 0                  # selected file index
        self._flisttop = 0              # first visible row (list scroll)
        self._ftex: dict = {}           # disk_path -> Texture2D | None (image previews)
        self._row_rects: list = []      # (index, Rectangle) for click-to-select
        self._tab_rects: dict = {}      # "chat"/"files" -> Rectangle (clickable tabs)

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
        self.screen = "chat"
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
        self._unload_ftex()

    def _unload_ftex(self) -> None:
        for tex in self._ftex.values():
            if tex is not None:
                pr.unload_texture(tex)
        self._ftex = {}

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

    # --- files browser -----------------------------------------------------

    def _toggle_screen(self) -> None:
        self.screen = "files" if self.screen == "chat" else "chat"
        if self.screen == "files":
            self._load_files()
        while pr.get_char_pressed() > 0:        # swallow the Tab-adjacent char buffer
            pass

    def _load_files(self) -> None:
        self._files = self.link.drive_files()
        if self._fsel >= len(self._files):
            self._fsel = max(0, len(self._files) - 1)
        self._flisttop = 0

    def _open_selected(self) -> None:
        if not self._files:
            return
        f = self._files[self._fsel]
        if f.kind == "webapp" and f.content:        # a pinned live URL
            _open_externally(f.content)
            self._status = f"opening {f.content[:42]}..."
            return
        disk = self.link.drive_export(f.path, f.content)   # materialize text if needed
        if disk:
            _open_externally(disk)                  # opens in the CEO's default Mac app
            self._status = f"opening {f.name}"
        else:
            self._status = "no on-disk file to open"

    def _update_files(self) -> None:
        n = len(self._files)
        wheel = pr.get_mouse_wheel_move()
        if wheel:
            self._flisttop = max(0, self._flisttop - int(wheel * 3))
        if n:
            if pr.is_key_pressed(pr.KEY_DOWN):
                self._fsel = min(n - 1, self._fsel + 1)
            if pr.is_key_pressed(pr.KEY_UP):
                self._fsel = max(0, self._fsel - 1)
        if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
            mp = pr.get_mouse_position()
            for i, rect in self._row_rects:
                if pr.check_collision_point_rec(mp, rect):
                    if i == self._fsel:             # click the selected row again = open
                        self._open_selected()
                    else:
                        self._fsel = i
                    break
        if pr.is_key_pressed(pr.KEY_ENTER) or pr.is_key_pressed(pr.KEY_O):
            self._open_selected()
        if pr.is_key_pressed(pr.KEY_R):             # refresh the listing
            self._load_files()
        if pr.is_key_pressed(pr.KEY_ESCAPE) or gamepad.pressed(gamepad.CIRCLE):
            self.screen = "chat"                    # Esc backs out to chat, not close

    def _file_texture(self, disk_path: str, max_w: int, max_h: int):
        """Lazily load + cache an image asset as a GPU texture, scaled to fit the
        preview pane. Loaded in draw() (main thread owns the GL context)."""
        if disk_path in self._ftex:
            return self._ftex[disk_path]
        import os
        tex = None
        if disk_path and os.path.isfile(disk_path):
            img = pr.load_image(disk_path)
            if img.width > 0 and img.height > 0:
                scale = min(1.0, max_w / img.width, max_h / img.height)
                if scale < 1.0:
                    pr.image_resize(img, int(img.width * scale), int(img.height * scale))
                tex = pr.load_texture_from_image(img)
            pr.unload_image(img)
        self._ftex[disk_path] = tex
        return tex

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

        # Tab (or clicking a header tab) switches between the chat and Files browser.
        if pr.is_key_pressed(pr.KEY_TAB):
            self._toggle_screen()
            return
        if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
            mp = pr.get_mouse_position()
            for name, rect in self._tab_rects.items():
                if pr.check_collision_point_rec(mp, rect):
                    if self.screen != name:
                        self.screen = name
                        if name == "files":
                            self._load_files()
                    return
        if self.screen == "files":
            self._update_files()
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
        self._draw_tabs(x, y + 42, w)
        pr.draw_line(x + PAD, y + 70, x + w - PAD, y + 70, GREEN_FAINT)

        # The Files browser replaces the chat body/input when active.
        if self.screen == "files":
            self._draw_files(x, y, w, h)
            self._draw_scanlines(x, y, w, h)
            return

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
        hint = f"ENTER send   ·   {talk}   ·   TAB files   ·   ESC close"
        pr.draw_text(hint, x + PAD, y + h - 26, 14, GREEN_DIM)

        self._draw_scanlines(x, y, w, h)

    # --- files browser draw ------------------------------------------------

    def _draw_tabs(self, x: int, y: int, w: int) -> None:
        """Clickable [ CHAT ] [ FILES ] tabs at the top-right of the header."""
        self._tab_rects = {}
        tx = x + w - PAD
        for name, lab in (("files", "FILES"), ("chat", "CHAT")):   # right-to-left
            txt = f" {lab} "
            tw = pr.measure_text(txt, 16) + 8
            rect = pr.Rectangle(tx - tw, y, tw, 23)
            active = (self.screen == name)
            pr.draw_rectangle_rec(rect, pr.Color(22, 54, 34, 255) if active else SCREEN)
            pr.draw_rectangle_lines_ex(rect, 1, GREEN if active else GREEN_FAINT)
            pr.draw_text(txt, int(rect.x + 4), int(rect.y + 3), 16,
                         GREEN if active else GREEN_DIM)
            self._tab_rects[name] = rect
            tx -= tw + 8

    def _draw_files(self, x: int, y: int, w: int, h: int) -> None:
        top = y + 84
        bottom = y + h - 46
        list_w = int((w - 3 * PAD) * 0.46)
        list_x = x + PAD
        prev_x = list_x + list_w + PAD
        prev_w = x + w - PAD - prev_x
        files = self._files
        self._row_rects = []

        if not files:
            pr.draw_text("Drive is empty — files agents save will show up here.",
                         list_x, top, FONT, GREEN_FAINT)
        else:
            rows = max(1, (bottom - top) // LINE_H)
            if self._fsel < self._flisttop:
                self._flisttop = self._fsel
            elif self._fsel >= self._flisttop + rows:
                self._flisttop = self._fsel - rows + 1
            self._flisttop = max(0, min(self._flisttop, max(0, len(files) - rows)))
            ty = top
            for i in range(self._flisttop, min(len(files), self._flisttop + rows)):
                f = files[i]
                rect = pr.Rectangle(list_x - 4, ty - 2, list_w + 8, LINE_H)
                self._row_rects.append((i, rect))
                if i == self._fsel:
                    pr.draw_rectangle_rec(rect, pr.Color(22, 54, 34, 255))
                label = f"[{f.kind}] {f.path}"
                while label and pr.measure_text(label, 17) > list_w - 12:
                    label = label[:-2]
                pr.draw_text(label, list_x, ty, 17, GREEN if i == self._fsel else GREEN_DIM)
                ty += LINE_H
            pr.draw_text(f"{len(files)} file(s)", list_x, bottom + 6, 13, GREEN_FAINT)
            pr.draw_line(prev_x - PAD // 2, top, prev_x - PAD // 2, bottom, GREEN_FAINT)
            self._draw_preview(files[self._fsel], prev_x, top, prev_w, bottom - top)

        footer = self._status or "↑↓ select   ·   Enter / O open on your computer   ·   R refresh   ·   Tab chat   ·   Esc back"
        pr.draw_text(footer, list_x, y + h - 26, 14,
                     AMBER if self._status else GREEN_DIM)

    def _draw_preview(self, f, px: int, py: int, pw: int, ph: int) -> None:
        pr.draw_text(f.path, px, py, 17, GREEN)
        meta = f"{f.kind} · {f.size}c · by {f.author_name} · {f.updated_at}"
        for wl in _wrap(meta, pw, 14)[:1]:
            pr.draw_text(wl, px, py + 22, 14, GREEN_FAINT)
        cy = py + 48
        if f.kind == "image":
            disk = self.link.drive_local_path(f.path)
            tex = self._file_texture(disk, pw, ph - 90) if disk else None
            if tex is not None:
                pr.draw_texture(tex, int(px), int(cy), pr.WHITE)
            else:
                pr.draw_text("[image — press Enter / O to open]", px, cy, 15, GREEN_FAINT)
        elif f.kind == "webapp" and f.content:
            pr.draw_text("Live link:", px, cy, 15, GREEN_DIM)
            for wl in _wrap(f.content, pw, 16):
                cy += 22
                pr.draw_text(wl, px, cy, 16, LINK)
        elif f.content:
            avail = max(1, (py + ph - 24 - cy) // 20)
            for wl in _wrap(f.content, pw, 16)[:avail]:
                pr.draw_text(wl, px, cy, 16, GREEN)
                cy += 20
        else:
            pr.draw_text("Binary file — press Enter / O to open in your Mac apps.",
                         px, cy, 15, GREEN_FAINT)

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

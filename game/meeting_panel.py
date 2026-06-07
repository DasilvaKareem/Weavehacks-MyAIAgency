"""In-game meeting overlay: invite agents, set a topic, watch it play out live.

Two modes:
  * invite — type a topic and click agents to add them to the meeting.
  * live   — the orchestrator runs on a worker thread; turns stream in from the
             RTDB channel (or SQLite) via MeetingLink.poll(). The full transcript
             is saved durably to SQLite either way.
"""
from __future__ import annotations

import pyray as pr

from .chat_panel import _wrap
from .audio import VoicePlayer

PANEL_W = 840
PANEL_H = 560
FONT = 18
LINE_H = 23
PAD = 18
ROW_H = 34

BG = pr.Color(18, 22, 32, 240)
BAR = pr.Color(120, 70, 160, 255)        # purple — distinct from chat's blue bar
FIELD = pr.Color(30, 36, 50, 255)
CEO_COLOR = pr.Color(245, 214, 120, 255)
SPEAKER_COLOR = pr.Color(190, 214, 240, 255)
DIM = pr.Color(0, 0, 0, 140)
GOOD = pr.Color(70, 200, 120, 255)
MUTED = pr.Color(150, 160, 180, 255)


def _btn(rect, label, base, enabled=True) -> bool:
    mouse = pr.get_mouse_position()
    hover = enabled and pr.check_collision_point_rec(mouse, rect)
    col = base if enabled else pr.Color(80, 80, 90, 255)
    if hover:
        col = pr.Color(min(col.r + 30, 255), min(col.g + 30, 255), min(col.b + 30, 255), 255)
    pr.draw_rectangle_rec(rect, col)
    pr.draw_rectangle_lines_ex(rect, 2, pr.Color(10, 12, 20, 255))
    tw = pr.measure_text(label, 18)
    pr.draw_text(label, int(rect.x + (rect.width - tw) / 2), int(rect.y + (rect.height - 18) / 2), 18, pr.RAYWHITE)
    return hover and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)


class MeetingPanel:
    def __init__(self, link, agents: list) -> None:
        self.link = link
        self.agents = agents          # live reference to the game's agent list
        self.open = False
        self.mode = "invite"          # invite | live
        self.topic = ""
        self.invited: set[str] = set()  # backend_ids
        self._lines: list[tuple[pr.Color, str]] = []
        self._scroll = 0
        self._invite_scroll = 0
        self._draft = ""               # what the CEO is typing into a live meeting
        self.voice_mode = "local"     # off | local (speak here) | daily (boardroom call)
        self.player = None            # VoicePlayer, created when a local voiced meeting starts
        self._daily_reason = None     # why a boardroom call can't start (dep/key), or None

    # --- lifecycle ---------------------------------------------------------

    def open_panel(self) -> None:
        self.open = True
        self.mode = "invite"
        self.topic = ""
        self.invited = set()
        self._lines = []
        self._scroll = 0
        self._invite_scroll = 0
        self._draft = ""
        self._daily_reason = self.link.daily_available()
        while pr.get_char_pressed() > 0:   # drop the 'M' that opened this
            pass

    def close(self) -> None:
        if self.player is not None:
            self.player.shutdown()
            self.player = None
        self.link.shutdown()
        self.open = False

    def _start(self) -> None:
        ids = [a.backend_id for a in self.agents if a.backend_id in self.invited]
        if len(ids) < 2 or not self.topic.strip():
            return
        mode = self.voice_mode
        if mode == "daily" and self._daily_reason:   # not configured — fall back
            mode = "local"
        self.voice_mode = mode
        self.link.start(self.topic.strip(), ids, mode="moderated", voice_mode=mode)
        self.mode = "live"
        self._lines = []
        self._scroll = 0
        self._draft = ""
        if mode == "local" and self.player is None:
            self.player = VoicePlayer()

    # --- per-frame ---------------------------------------------------------

    def update(self) -> None:
        if not self.open:
            return
        if pr.is_key_pressed(pr.KEY_ESCAPE):
            self.close()
            return

        if self.mode == "invite":
            ch = pr.get_char_pressed()
            while ch > 0:
                if 32 <= ch < 127 and len(self.topic) < 80:
                    self.topic += chr(ch)
                ch = pr.get_char_pressed()
            bs = pr.is_key_pressed(pr.KEY_BACKSPACE)
            if hasattr(pr, "is_key_pressed_repeat"):
                bs = bs or pr.is_key_pressed_repeat(pr.KEY_BACKSPACE)
            if bs and self.topic:
                self.topic = self.topic[:-1]
            wheel = pr.get_mouse_wheel_move()
            if wheel:
                self._invite_scroll = max(0, self._invite_scroll - int(wheel))
        else:  # live
            for name, content in self.link.poll():
                color = CEO_COLOR if name == "CEO" else SPEAKER_COLOR
                for wl in _wrap(f"{name}: {content}", PANEL_W - 2 * PAD, FONT):
                    self._lines.append((color, wl))
                self._lines.append((BG, ""))   # spacer
                # Voice everyone but the CEO — the human already typed their line,
                # no need for the machine to read it back to them.
                if self.voice_mode == "local" and self.player is not None and name != "CEO":
                    self.player.enqueue(content, self.link.voice_for(name))
            wheel = pr.get_mouse_wheel_move()
            if wheel:
                self._scroll = max(0, self._scroll + int(wheel * 3))
            # While the call is live, the CEO can type a line and Enter to send it
            # — it's folded into the meeting before the next agent turn.
            if self.link.running():
                ch = pr.get_char_pressed()
                while ch > 0:
                    if 32 <= ch < 127 and len(self._draft) < 160:
                        self._draft += chr(ch)
                    ch = pr.get_char_pressed()
                bs = pr.is_key_pressed(pr.KEY_BACKSPACE)
                if hasattr(pr, "is_key_pressed_repeat"):
                    bs = bs or pr.is_key_pressed_repeat(pr.KEY_BACKSPACE)
                if bs and self._draft:
                    self._draft = self._draft[:-1]
                if pr.is_key_pressed(pr.KEY_ENTER) and self._draft.strip():
                    self.link.say(self._draft.strip())
                    self._draft = ""

    # --- draw --------------------------------------------------------------

    def draw(self) -> None:
        if not self.open:
            return
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pr.draw_rectangle(0, 0, sw, sh, DIM)
        x, y = (sw - PANEL_W) // 2, (sh - PANEL_H) // 2
        pr.draw_rectangle(x, y, PANEL_W, PANEL_H, BG)
        pr.draw_rectangle(x, y, PANEL_W, 44, BAR)
        if self.mode == "invite":
            self._draw_invite(x, y)
        else:
            self._draw_live(x, y)

    def _draw_invite(self, x: int, y: int) -> None:
        pr.draw_text("Hold a Meeting", x + PAD, y + 11, 22, pr.RAYWHITE)

        # Topic field
        pr.draw_text("Topic", x + PAD, y + 58, 15, MUTED)
        field = pr.Rectangle(x + PAD, y + 78, PANEL_W - 2 * PAD, 34)
        pr.draw_rectangle_rec(field, FIELD)
        pr.draw_rectangle_lines_ex(field, 1, pr.Color(120, 70, 160, 255))
        caret = "_" if (pr.get_time() % 1.0) < 0.5 else ""
        pr.draw_text((self.topic or "Type the meeting topic…") + (caret if self.topic else ""),
                     int(field.x) + 8, int(field.y) + 8, FONT,
                     pr.RAYWHITE if self.topic else pr.GRAY)

        # Attendee list with checkboxes
        pr.draw_text("Invite agents (click to toggle)", x + PAD, y + 124, 15, MUTED)
        list_top = y + 148
        list_h = PANEL_H - (list_top - y) - 78
        rows_fit = max(1, list_h // ROW_H)
        roster = list(self.agents)
        self._invite_scroll = min(self._invite_scroll, max(0, len(roster) - rows_fit))
        view = roster[self._invite_scroll:self._invite_scroll + rows_fit]
        mouse = pr.get_mouse_position()
        for i, a in enumerate(view):
            ry = list_top + i * ROW_H
            row = pr.Rectangle(x + PAD, ry, PANEL_W - 2 * PAD, ROW_H - 6)
            inv = a.backend_id in self.invited
            pr.draw_rectangle_rec(row, pr.Color(40, 48, 66, 255) if inv else pr.Color(28, 32, 44, 255))
            box = pr.Rectangle(row.x + 8, ry + 5, 18, 18)
            pr.draw_rectangle_lines_ex(box, 2, GOOD if inv else MUTED)
            if inv:
                pr.draw_rectangle(int(box.x) + 4, int(box.y) + 4, 10, 10, GOOD)
            pr.draw_text(f"{a.name}", int(box.x) + 30, ry + 4, FONT, pr.RAYWHITE)
            rt = f"{a.role}"
            pr.draw_text(rt, int(row.x + row.width) - pr.measure_text(rt, 15) - 10, ry + 6, 15, SPEAKER_COLOR)
            if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT) and pr.check_collision_point_rec(mouse, row):
                self.invited.discard(a.backend_id) if inv else self.invited.add(a.backend_id)

        # Voice-output selector: Off / Speak aloud (here) / Boardroom Call (Daily)
        vy = y + PANEL_H - 94
        pr.draw_text("Voice:", x + PAD, vy + 1, 16, MUTED)
        ox = x + PAD + 62
        for key, label in (("off", "Off"), ("local", "Speak aloud"),
                           ("daily", "Boardroom Call")):
            enabled = key != "daily" or self._daily_reason is None
            sel = self.voice_mode == key
            cx, cy = ox + 8, int(vy) + 9
            pr.draw_circle(cx, cy, 8, GOOD if sel else pr.Color(40, 46, 62, 255))
            pr.draw_circle_lines(cx, cy, 8, GOOD if sel else MUTED)
            tcol = pr.RAYWHITE if enabled else pr.Color(110, 112, 122, 255)
            pr.draw_text(label, ox + 22, int(vy) + 1, 16, tcol)
            w = 22 + pr.measure_text(label, 16) + 26
            hit = pr.Rectangle(ox, vy - 4, w, 26)
            if enabled and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT) \
                    and pr.check_collision_point_rec(mouse, hit):
                self.voice_mode = key
            ox += w
        if self._daily_reason:
            pr.draw_text("Boardroom Call needs setup: " + self._daily_reason,
                         x + PAD, vy + 22, 12, pr.Color(210, 160, 120, 255))

        # Footer buttons
        n = len(self.invited)
        can_start = n >= 2 and bool(self.topic.strip())
        verb = "Start Boardroom Call" if self.voice_mode == "daily" else "Start Meeting"
        cancel = pr.Rectangle(x + PAD, y + PANEL_H - 56, 130, 40)
        start = pr.Rectangle(x + PANEL_W - PAD - 250, y + PANEL_H - 56, 250, 40)
        if _btn(cancel, "Cancel", pr.Color(120, 60, 60, 255)):
            self.close()
        if _btn(start, f"{verb} ({n})", pr.Color(80, 120, 70, 255), enabled=can_start) and can_start:
            self._start()

    def _draw_live(self, x: int, y: int) -> None:
        prefix = "Boardroom Call: " if self.voice_mode == "daily" else "Meeting: "
        title = prefix + self.link.topic
        if pr.measure_text(title, 20) > PANEL_W - 2 * PAD:
            title = title[:60] + "…"
        pr.draw_text(title, x + PAD, y + 12, 20, pr.RAYWHITE)
        if self.voice_mode == "daily":
            url = self.link.room_url
            line = ("🎙️ live in your browser — just speak to join in  ·  " + url) if url else \
                "📞 starting call — opening your browser…"
            if pr.measure_text(line, 14) > PANEL_W - 2 * PAD:
                line = line[:96] + "…"
            pr.draw_text(line, x + PAD, y + 38, 14, GOOD if url else MUTED)

        body_top = y + 58
        foot = y + PANEL_H - 56
        running = self.link.running()
        # While the call is live, reserve a row above the footer for the CEO's
        # input field; once it's over, the transcript reclaims that space.
        input_y = foot - 44
        body_bottom = (input_y - 6) if running else foot
        visible = max(1, (body_bottom - body_top) // LINE_H)
        max_scroll = max(0, len(self._lines) - visible)
        self._scroll = min(self._scroll, max_scroll)
        end = len(self._lines) - self._scroll
        ty = body_top
        for color, line in self._lines[max(0, end - visible):end]:
            if line:
                pr.draw_text(line, x + PAD, ty, FONT, color)
            ty += LINE_H

        if running:
            fld = pr.Rectangle(x + PAD, input_y, PANEL_W - 2 * PAD, 32)
            pr.draw_rectangle_rec(fld, FIELD)
            pr.draw_rectangle_lines_ex(fld, 1, pr.Color(120, 70, 160, 255))
            caret = "_" if (pr.get_time() % 1.0) < 0.5 else ""
            placeholder = "Speak up — type to steer the team, Enter to send…"
            pr.draw_text((self._draft or placeholder) + (caret if self._draft else ""),
                         int(fld.x) + 8, int(fld.y) + 7, FONT,
                         pr.RAYWHITE if self._draft else pr.GRAY)
        if running:
            dots = "." * (1 + int(pr.get_time() * 2) % 3)
            pr.draw_text("in progress" + dots, x + PAD, foot + 8, FONT, MUTED)
        else:
            pr.draw_text("✓ meeting ended · transcript saved", x + PAD, foot + 8, FONT, GOOD)
        if self._scroll > 0:
            pr.draw_text("^ older", x + PANEL_W - PAD - pr.measure_text("^ older", 13), body_top - 2, 13, MUTED)
        if self.voice_mode == "daily":
            url = self.link.room_url
            if _btn(pr.Rectangle(x + PANEL_W - PAD - 270, foot, 140, 40),
                    "Re-open room", pr.Color(80, 120, 70, 255), enabled=bool(url)) and url:
                import webbrowser
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
        else:
            vlabel = "Voice: ON" if self.voice_mode == "local" else "Voice: OFF"
            vcol = pr.Color(80, 120, 70, 255) if self.voice_mode == "local" else pr.Color(90, 70, 70, 255)
            if _btn(pr.Rectangle(x + PANEL_W - PAD - 270, foot, 140, 40), vlabel, vcol):
                self._toggle_voice()
        if _btn(pr.Rectangle(x + PANEL_W - PAD - 120, foot, 120, 40), "Close", pr.Color(70, 90, 130, 255)):
            self.close()

    def _toggle_voice(self) -> None:
        """Live toggle for non-Daily meetings: flip local speech on/off."""
        if self.voice_mode == "local":
            self.voice_mode = "off"
            if self.player is not None:
                self.player.clear()             # stop queued speech immediately
        else:
            self.voice_mode = "local"
            if self.player is None:
                self.player = VoicePlayer()

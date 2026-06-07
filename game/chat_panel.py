"""In-game chat overlay: talk to one agent, one-on-one.

Opened by walking up to an agent and pressing F (see main.py). While open it
captures keyboard text entry; the game freezes player/camera input so typing
doesn't move the CEO. Model calls go through CompanyLink on a worker thread, so
the panel only ever polls — it never blocks the render loop.
"""
from __future__ import annotations

import os
import re
import subprocess

import pyray as pr

from . import gamepad, voice
from backend.config import (GEMINI_MODEL, role_uses_image_gen, role_uses_video_gen,
                            role_connect_toolkits)
from backend.persona import generate as make_persona

# How an agent's "working" state reads while we wait, by capability. Video
# (Veo) is the slow one, so it gets an explicit heads-up.
SPINNER = "|/-\\"

# Pull generated-media paths out of an agent reply (the agent reports where it
# saved). Designer -> .png (shown inline); Animator -> .mp4 (click to play).
_IMG_PATH_RE = re.compile(r"([^\s`'\"]+\.png)")
_VID_PATH_RE = re.compile(r"([^\s`'\"]+\.mp4)")
# Width of the in-panel column reserved for a generated-media preview.
PREVIEW_COL = 230


def _open_externally(path: str) -> None:
    """Open a file in the OS default app (mp4 -> system video player)."""
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif _HAS_OPEN:
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


_HAS_OPEN = os.path.exists("/usr/bin/open")

# Friendly names for Composio Google toolkits shown on the Connect banner.
_APP_NAME = {"gmail": "Gmail", "googlecalendar": "Calendar", "googledrive": "Drive",
             "googledocs": "Docs", "googlesheets": "Sheets", "vercel": "Vercel"}


def _app_name(tk: str) -> str:
    return _APP_NAME.get(tk.lower(), tk.replace("_", " ").title())


def _draw_btn(rect, label: str, base) -> None:
    hover = pr.check_collision_point_rec(pr.get_mouse_position(), rect)
    col = pr.Color(min(base.r + 30, 255), min(base.g + 30, 255),
                   min(base.b + 30, 255), 255) if hover else base
    pr.draw_rectangle_rec(rect, col)
    tw = pr.measure_text(label, 14)
    pr.draw_text(label, int(rect.x + (rect.width - tw) / 2), int(rect.y + 4), 14, pr.RAYWHITE)

PANEL_W = 760
PANEL_H = 480
FONT = 18
LINE_H = 23
PAD = 16
MAX_INPUT = 300

BG = pr.Color(18, 22, 32, 235)
BAR = pr.Color(34, 92, 168, 255)
INPUT_BG = pr.Color(30, 36, 50, 255)
CEO_COLOR = pr.Color(245, 214, 120, 255)
AGENT_COLOR = pr.Color(190, 214, 240, 255)
DIM = pr.Color(0, 0, 0, 120)


def _break_token(word: str, max_w: int, font: int) -> list[str]:
    """Hard-split a single word that's wider than max_w (e.g. a long file path),
    so it can't overrun the panel. Returns one or more pieces that each fit."""
    if pr.measure_text(word, font) <= max_w:
        return [word]
    pieces, cur = [], ""
    for chcode in word:
        if pr.measure_text(cur + chcode, font) <= max_w or not cur:
            cur += chcode
        else:
            pieces.append(cur)
            cur = chcode
    if cur:
        pieces.append(cur)
    return pieces


def _wrap(text: str, max_w: int, font: int) -> list[str]:
    """Greedy word-wrap to a pixel width, hard-breaking overlong tokens."""
    out: list[str] = []
    for para in text.split("\n"):
        line = ""
        for raw in para.split(" "):
            for word in _break_token(raw, max_w, font):
                trial = word if not line else line + " " + word
                if pr.measure_text(trial, font) <= max_w or not line:
                    line = trial
                else:
                    out.append(line)
                    line = word
        out.append(line)
    return out


class ChatPanel:
    def __init__(self, link) -> None:
        self.link = link
        self.agent = None          # the Character we're talking to, or None
        # Set by Game: handler(agent, text) -> ack str if `text` was a movement
        # command (already applied to the bot), else None to send to the model.
        self.command_handler = None
        self.input = ""
        self.waiting = False
        self._lines: list[tuple[pr.Color, str]] = []   # cached wrapped log
        self.voice = voice.VoiceInput(GEMINI_MODEL)    # push-to-talk (no-op w/o mic)
        self._status = ""          # transient line shown in the input box
        self._scroll = 0           # lines scrolled up from the bottom (0 = newest)
        self._blurb = ""           # persona one-liner for the header
        self._voice_name = None    # macOS `say` voice for this agent
        self._textures: dict[str, object] = {}  # png path -> loaded Texture2D
        self._preview_path = None  # most-recent generated image to show in-panel
        self._video_path = None    # most-recent generated video (click to play)
        self._play_rect = None     # clickable "play video" card, set during draw
        self._wait_start = 0.0     # pr.get_time() when the current wait began
        self._step = ""            # live tool-loop activity ("using X"), if any
        self._partial = ""         # streamed final-answer text so far, if any
        # Composio (Google apps) connection state for this agent.
        self._toolkits: list = []          # toolkits this agent's role uses
        self._conn_status: dict = {}       # toolkit -> active|expired|missing|unknown
        self._connecting: set = set()      # toolkits whose auth URL is being fetched
        self._connect_rect = None
        self._recheck_rect = None

    @property
    def open(self) -> bool:
        return self.agent is not None

    def _panel_xy(self) -> tuple[int, int]:
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        return (sw - PANEL_W) // 2, (sh - PANEL_H) // 2

    def _close_rect(self) -> pr.Rectangle:
        x, y = self._panel_xy()
        return pr.Rectangle(x + PANEL_W - 38, y + 7, 26, 26)

    # --- lifecycle ---------------------------------------------------------

    def open_with(self, character) -> None:
        self.agent = character
        self.input = ""
        self.waiting = self.link.is_busy(character.backend_id)
        # Reconnecting to a job already in flight: we don't know its true start,
        # so show the elapsed timer from "now" rather than a wrong number.
        self._wait_start = pr.get_time() if self.waiting else 0.0
        character.status = "working" if self.waiting else "idle"
        # Persona blurb (UI) + a stable unique TTS voice, both keyed by agent id.
        persona = make_persona(character.backend_id, character.role)
        self._blurb = persona.blurb()
        self._voice_name = voice.pick_voice(character.backend_id)
        # If this agent runs on Composio (Google apps), check whether they're
        # connected so we can offer an in-game "Connect" button.
        self._toolkits = role_connect_toolkits(character.role)
        self._conn_status = {}
        self._connecting = set()
        if self._toolkits:
            self.link.request_composio_status(character.backend_id, self._toolkits)
        while pr.get_char_pressed() > 0:    # drop the 'f' that opened this panel
            pass
        self._refresh()

    def close(self) -> None:
        self.voice.cancel()
        voice.stop_speaking()
        self._unload_textures()
        self.agent = None
        self.input = ""
        self.waiting = False
        self._step = ""
        self._partial = ""
        self._status = ""
        self._lines = []
        self._preview_path = None
        self._video_path = None
        self._play_rect = None

    # --- generated-image previews -----------------------------------------

    def _unload_textures(self) -> None:
        for tex in self._textures.values():
            pr.unload_texture(tex)
        self._textures.clear()

    def _texture_for(self, path: str):
        """Lazily load (and cache) a PNG as a GPU texture. None if unloadable.

        Safe to call here: draw()/update() run on the main thread that owns the
        GL context, so texture upload is legal. Bad paths cache as None so we
        don't retry a missing file every frame.
        """
        if path in self._textures:
            return self._textures[path]
        tex = None
        if os.path.isfile(path):
            img = pr.load_image(path)
            if img.width > 0 and img.height > 0:
                # Downscale big generations to fit the in-panel preview column.
                scale = min(1.0, PREVIEW_COL / max(img.width, img.height))
                if scale < 1.0:
                    pr.image_resize(img, int(img.width * scale), int(img.height * scale))
                tex = pr.load_texture_from_image(img)
            pr.unload_image(img)
        self._textures[path] = tex
        return tex

    def _scan_for_images(self) -> None:
        """Find the newest generated image/video referenced in this history."""
        newest_img = newest_vid = None
        for m in self.link.history(self.agent.backend_id):
            if m.role != "ai":
                continue
            for match in _IMG_PATH_RE.findall(m.content):
                if os.path.isfile(match):
                    newest_img = match
            for match in _VID_PATH_RE.findall(m.content):
                if os.path.isfile(match):
                    newest_vid = match
        self._preview_path = newest_img
        self._video_path = newest_vid

    def _submit(self, text: str) -> None:
        """Send `text` to the agent (shared by typing and voice)."""
        text = text.strip()
        if not text:
            return
        # Movement commands ("go to the meeting room", "follow me", ...) are caught
        # here and handed to the bot instead of the model; the chat closes so the
        # bot is released to actually walk off and do it.
        if self.command_handler is not None:
            ack = self.command_handler(self.agent, text)
            if ack is not None:
                vname = self._voice_name
                self.close()
                voice.speak(ack, vname)        # speak the ack aloud, then release the bot
                return
        if self.link.send(self.agent.backend_id, text.strip()):
            self.input = ""
            self.waiting = True
            self._step = ""            # fresh turn: clear last turn's activity
            self._partial = ""         # ...and any leftover streamed text
            self._wait_start = pr.get_time()
            self.agent.status = "working"
            self._refresh(pending=True)

    # --- waiting-state copy -------------------------------------------------

    def _wait_verb(self) -> str:
        """What the agent is doing: the live tool step if we have one, else a
        capability-tuned default while it's still thinking before any tool call."""
        if self._step:                # real activity reported by the tool loop
            return self._step
        role = self.agent.role
        if role_uses_video_gen(role):
            return "animating"
        if role_uses_image_gen(role):
            return "designing"
        return "thinking"

    def _wait_line(self) -> str:
        """Animated status line shown while a reply is pending."""
        spin = SPINNER[int(pr.get_time() * 8) % len(SPINNER)]
        elapsed = max(0, int(pr.get_time() - self._wait_start)) if self._wait_start else 0
        verb = self._wait_verb()
        clock = f"  ({elapsed}s)" if elapsed else ""
        slow = "  -  video can take a few minutes" if verb == "animating" else ""
        return f"{spin} {self.agent.name} is {verb}...{clock}{slow}"

    # --- push-to-talk ------------------------------------------------------

    def _update_voice(self) -> None:
        """Hold R2 / Left-Ctrl to record; release to transcribe and auto-send."""
        held = gamepad.down(gamepad.R2) or pr.is_key_down(pr.KEY_LEFT_CONTROL)
        if held and not self.voice.recording and not self.voice.transcribing:
            self.voice.begin()
            self._status = "🎙  listening…"
        elif self.voice.recording and not held:
            self.voice.end()
            self._status = "transcribing…" if self.voice.transcribing else ""

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

        wheel = pr.get_mouse_wheel_move()    # scroll back through history
        if wheel:
            self._scroll = max(0, self._scroll + int(wheel * 3))

        if self.waiting:
            # Drain progress first: poll_reply drops the queues once done.
            step = self.link.poll_steps(self.agent.backend_id)
            if step:
                self._step = step
            for tok in self.link.poll_tokens(self.agent.backend_id):
                if tok is None:        # tool round: discard its streamed preamble
                    self._partial = ""
                else:
                    self._partial += tok
            reply = self.link.poll_reply(self.agent.backend_id)
            if reply is not None:
                self.waiting = False
                self._step = ""
                self._partial = ""     # the full reply is now in the store log
                self.agent.status = "idle"
                self._refresh()
                if not reply.startswith("[error"):
                    voice.speak(reply, self._voice_name)   # agent talks back, in its own voice

        # Click the "play video" card to open the clip in the system player.
        if (self._video_path and self._play_rect is not None
                and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)
                and pr.check_collision_point_rec(pr.get_mouse_position(), self._play_rect)):
            _open_externally(self._video_path)
            return

        # Close on Esc, controller Circle, or clicking the ✕ in the title bar.
        clicked_x = (pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)
                     and pr.check_collision_point_rec(pr.get_mouse_position(), self._close_rect()))
        if pr.is_key_pressed(pr.KEY_ESCAPE) or gamepad.pressed(gamepad.CIRCLE) or clicked_x:
            self.close()
            return

        self._update_connect()              # Composio status + Connect/Re-check buttons

        self._update_voice()                # push-to-talk works even while waiting
        if self.waiting or self.voice.recording or self.voice.transcribing:
            return                          # ignore typing while busy

        # CEO rates the agent's last reply → logged as Weave feedback on that exact
        # call, so the People Analytics Lead can rank agents by how the CEO actually
        # likes them. F-keys, so they never land in the text box.
        if pr.is_key_pressed(pr.KEY_F1):
            self._status = ("👍 logged — People Analytics will see it"
                            if self.link.react(self.agent.backend_id, True)
                            else "nothing to rate yet")
        elif pr.is_key_pressed(pr.KEY_F2):
            self._status = ("👎 logged — People Analytics will see it"
                            if self.link.react(self.agent.backend_id, False)
                            else "nothing to rate yet")

        ch = pr.get_char_pressed()
        while ch > 0:
            if 32 <= ch < 127 and len(self.input) < MAX_INPUT:
                self.input += chr(ch)
                self._status = ""           # typing clears a lingering voice status
            ch = pr.get_char_pressed()

        backspace = pr.is_key_pressed(pr.KEY_BACKSPACE)
        if hasattr(pr, "is_key_pressed_repeat"):
            backspace = backspace or pr.is_key_pressed_repeat(pr.KEY_BACKSPACE)
        if backspace and self.input:
            self.input = self.input[:-1]

        if pr.is_key_pressed(pr.KEY_ENTER) and self.input.strip():
            self._submit(self.input)

    # --- Composio connect (Google apps) ------------------------------------

    def _update_connect(self) -> None:
        """Poll connection status, open any ready auth URLs, handle button clicks."""
        if not self._toolkits:
            return
        st = self.link.poll_composio_status(self.agent.backend_id)
        if st is not None:
            self._conn_status = st
        for tk in list(self._connecting):          # open auth URLs as they arrive
            url = self.link.poll_connect(tk)
            if url:
                _open_externally(url)
                self._connecting.discard(tk)
                self._status = f"opening browser to authorize {_app_name(tk)}…"
        if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
            m = pr.get_mouse_position()
            if self._connect_rect and pr.check_collision_point_rec(m, self._connect_rect):
                self._start_connect()
            elif self._recheck_rect and pr.check_collision_point_rec(m, self._recheck_rect):
                self.link.composio_refresh()
                self.link.request_composio_status(self.agent.backend_id, self._toolkits)
                self._conn_status, self._status = {}, "re-checking connections…"

    def _start_connect(self) -> None:
        """Request an auth URL for each not-yet-active toolkit; they open as ready."""
        started = False
        for tk in self._toolkits:
            if self._conn_status.get(tk.lower()) != "active" and self.link.request_connect(tk):
                self._connecting.add(tk)
                started = True
        if started:
            self._status = "generating sign-in link…"

    def _draw_connect_banner(self, x: int, y: int) -> int:
        """A strip under the title bar showing Google-app connection + buttons.
        Returns its height (0 if this agent doesn't use Composio)."""
        self._connect_rect = self._recheck_rect = None
        if not self._toolkits:
            return 0
        h = 30
        pr.draw_rectangle(x, y, PANEL_W, h, pr.Color(28, 34, 48, 255))
        st = self._conn_status
        missing = [t for t in self._toolkits if st.get(t.lower()) != "active"]
        if not st:
            msg, col = "Checking Google access…", pr.Color(180, 190, 210, 255)
        elif not missing:
            msg, col = "Google apps connected", pr.Color(120, 210, 150, 255)
        else:
            apps = ", ".join(_app_name(t) for t in missing)
            msg, col = f"Sign-in needed: {apps}", pr.Color(240, 200, 120, 255)
        pr.draw_text(msg, x + PAD, y + 7, 15, col)
        if st:                                      # buttons only once we know status
            bx = x + PANEL_W - PAD
            self._recheck_rect = pr.Rectangle(bx - 90, y + 4, 90, 22)
            _draw_btn(self._recheck_rect, "Re-check", pr.Color(70, 90, 130, 255))
            if missing:
                label = "Opening…" if self._connecting else "Connect"
                self._connect_rect = pr.Rectangle(bx - 90 - 8 - 96, y + 4, 96, 22)
                _draw_btn(self._connect_rect, label, pr.Color(70, 130, 80, 255))
        return h

    def _refresh(self, pending: bool = False) -> None:
        """Rebuild the wrapped conversation log from the store (event-driven)."""
        self._scan_for_images()              # surface any generated media first
        # Narrow the text column when a media preview occupies the top-right.
        has_media = self._preview_path or self._video_path
        body_w = PANEL_W - 2 * PAD - (PREVIEW_COL + PAD if has_media else 0)
        lines: list[tuple[pr.Color, str]] = []
        for m in self.link.history(self.agent.backend_id):
            who = "You" if m.role == "human" else self.agent.name
            color = CEO_COLOR if m.role == "human" else AGENT_COLOR
            for i, wl in enumerate(_wrap(f"{who}: {m.content}", body_w, FONT)):
                lines.append((color, wl))
            lines.append((BG, ""))          # blank spacer between turns
        # The animated "is working…" line is drawn live in draw() (so its spinner
        # and timer tick), not baked into the static cache here.
        self._lines = lines
        self._scroll = 0                     # snap to newest on any new message

    # --- draw --------------------------------------------------------------

    def draw(self) -> None:
        if not self.open:
            return
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pr.draw_rectangle(0, 0, sw, sh, DIM)

        x = (sw - PANEL_W) // 2
        y = (sh - PANEL_H) // 2
        pr.draw_rectangle(x, y, PANEL_W, PANEL_H, BG)
        pr.draw_rectangle(x, y, PANEL_W, 52, BAR)
        a = self.agent
        title = f"{a.name}  —  {a.role}" + (f" · {a.dept}" if a.dept else "")
        pr.draw_text(title, x + PAD, y + 6, 20, pr.RAYWHITE)
        sub = self._blurb + (f"   ·   voiced by {self._voice_name}" if self._voice_name else "")
        if sub.strip():
            pr.draw_text(sub, x + PAD, y + 31, 14, pr.Color(155, 200, 235, 255))

        # Close (✕) button in the title bar — click to leave the chat.
        cr = self._close_rect()
        hover = pr.check_collision_point_rec(pr.get_mouse_position(), cr)
        pr.draw_rectangle_rec(cr, pr.Color(200, 70, 70, 255) if hover else pr.Color(120, 40, 44, 255))
        xw = pr.measure_text("X", 18)
        pr.draw_text("X", int(cr.x + (cr.width - xw) / 2), int(cr.y + 4), 18, pr.RAYWHITE)

        # Composio connect banner sits just under the title bar (0 height if N/A).
        banner_h = self._draw_connect_banner(x, y + 52)

        # conversation body — scroll-aware: _scroll lines up from the bottom
        body_top = y + 60 + banner_h
        input_y = y + PANEL_H - 44
        visible = max(1, (input_y - body_top) // LINE_H)
        max_scroll = max(0, len(self._lines) - visible)
        if self._scroll > max_scroll:
            self._scroll = max_scroll
        end = len(self._lines) - self._scroll
        window = self._lines[max(0, end - visible):end]
        ty = body_top
        for color, line in window:
            if line:
                pr.draw_text(line, x + PAD, ty, FONT, color)
            ty += LINE_H
        # While waiting (and at the bottom): stream the answer live once tokens
        # arrive, otherwise show the animated "is working…"/tool-step line.
        if self.waiting and self._scroll == 0:
            text_w = (PANEL_W - 2 * PAD
                      - (PREVIEW_COL + PAD if (self._preview_path or self._video_path) else 0))
            if self._partial:
                cursor = "▌" if int(pr.get_time() * 2) % 2 == 0 else ""
                body = f"{self.agent.name}: {self._partial}{cursor}"
                for wl in _wrap(body, text_w, FONT):
                    if ty >= input_y - LINE_H:
                        break              # clip the tail; the full reply lands in the log
                    pr.draw_text(wl, x + PAD, ty, FONT, AGENT_COLOR)
                    ty += LINE_H
            elif ty < input_y - LINE_H:
                for wl in _wrap(self._wait_line(), text_w, FONT):
                    pr.draw_text(wl, x + PAD, ty, FONT, pr.Color(120, 200, 255, 255))
                    ty += LINE_H
        if self._scroll > 0:                 # show you're not at the latest
            tag = "^ older  (scroll down for latest)"
            tw = pr.measure_text(tag, 12)
            pr.draw_text(tag, x + PANEL_W - PAD - tw, body_top - 2, 12,
                         pr.Color(150, 170, 200, 255))

        # input box
        rec = self.voice.recording
        box = pr.Color(120, 40, 44, 255) if rec else INPUT_BG
        pr.draw_rectangle(x + PAD, input_y, PANEL_W - 2 * PAD, 30, box)
        if self._status:
            pr.draw_text(self._status, x + PAD + 8, input_y + 6, FONT, pr.Color(245, 200, 120, 255))
        elif self.waiting:
            note = "you can keep walking around - reply will be here when you return  (Esc to leave)"
            pr.draw_text(note, x + PAD + 8, input_y + 6, 15, pr.GRAY)
        else:
            caret = "_" if (pr.get_time() % 1.0) < 0.5 else " "
            pr.draw_text("> " + self.input + caret, x + PAD + 8, input_y + 6, FONT, pr.RAYWHITE)

        talk = "Hold R2 / Ctrl to talk" if voice.available() else "(mic unavailable)"
        hint = f"Enter send   ·   {talk}   ·   F1 👍 / F2 👎   ·   Esc / ○ / ✕  close"
        pr.draw_text(hint, x + PAD, y + PANEL_H - 12, 14, pr.LIGHTGRAY)

        self._draw_preview(x, y)

    def _clip_name(self, path: str, font: int) -> str:
        name = os.path.basename(path)
        while name and pr.measure_text(name, font) > PREVIEW_COL:
            name = name[:-1]
        return name

    def _draw_preview(self, panel_x: int, panel_y: int) -> None:
        """Show the latest generated media in the panel's top-right column:
        images render as a thumbnail; videos as a clickable 'play' card."""
        self._play_rect = None
        if not (self._preview_path or self._video_path):
            return

        col_x = panel_x + PANEL_W - PAD - PREVIEW_COL    # text was wrapped to clear it
        y = panel_y + 60

        # --- generated image thumbnail ---
        if self._preview_path:
            tex = self._texture_for(self._preview_path)
            if tex is not None:
                img_x = col_x + (PREVIEW_COL - tex.width) // 2
                pr.draw_text("Artwork", col_x, y, 13, pr.Color(155, 200, 235, 255))
                y += 18
                pr.draw_rectangle(img_x - 3, y - 3, tex.width + 6, tex.height + 6, BAR)
                pr.draw_texture(tex, img_x, y, pr.WHITE)
                pr.draw_text(self._clip_name(self._preview_path, 11), col_x,
                             y + tex.height + 6, 11, pr.Color(150, 170, 200, 255))
                y += tex.height + 26

        # --- generated video: clickable play card (no in-panel playback) ---
        if self._video_path:
            pr.draw_text("Video", col_x, y, 13, pr.Color(155, 200, 235, 255))
            y += 18
            card_h = 96
            rect = pr.Rectangle(col_x, y, PREVIEW_COL, card_h)
            self._play_rect = rect
            hover = pr.check_collision_point_rec(pr.get_mouse_position(), rect)
            pr.draw_rectangle_rec(rect, pr.Color(40, 48, 66, 255) if hover else pr.Color(28, 34, 48, 255))
            pr.draw_rectangle_lines_ex(rect, 2, BAR)
            # Play triangle in a circle.
            cx, cy = int(col_x + PREVIEW_COL / 2), int(y + card_h / 2 - 8)
            pr.draw_circle(cx, cy, 22, pr.Color(70, 130, 220, 255) if hover else pr.Color(50, 100, 190, 255))
            pr.draw_triangle(
                pr.Vector2(cx - 7, cy - 11), pr.Vector2(cx - 7, cy + 11),
                pr.Vector2(cx + 12, cy), pr.RAYWHITE,
            )
            label = "Click to play"
            lw = pr.measure_text(label, 13)
            pr.draw_text(label, int(col_x + (PREVIEW_COL - lw) / 2), int(y + card_h - 22),
                         13, pr.RAYWHITE)
            pr.draw_text(self._clip_name(self._video_path, 11), col_x, int(y + card_h + 4),
                         11, pr.Color(150, 170, 200, 255))

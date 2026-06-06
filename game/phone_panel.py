"""A drawable 90s Nokia phone — the CEO's command center.

Pull it up (N) — anywhere, in the office or out in the city; press N again to put it
away — and without walking anywhere you can:
  * New Message  → text your co-founder, the coordinator that plans the work,
    delegates to the team, and texts back one result (see coordinator_link.py).
  * Contacts     → pick any hired agent and Message them 1:1 (same backend as
    the walk-up chat) or Call them (the agent greets you in their own voice).

The phone body + green LCD are drawn from primitives (no sprite needed yet); a
sprite can replace _draw_body later without touching the screen logic, which all
renders inside the LCD rectangle. Like the other panels it only polls the links,
never blocking the render loop.
"""
from __future__ import annotations

import pyray as pr

from . import gamepad, voice, tasks
from .chat_panel import _wrap
from .coordinator_link import COFOUNDER_NAME
from backend.config import GEMINI_MODEL  # noqa: F401  (kept for parity / future use)

# Screens (a tiny state machine).
HOME, COFOUNDER, CONTACTS, AGENT, CALL = "home", "cofounder", "contacts", "agent", "call"
INBOX, MESSAGE, TODO = "inbox", "message", "todo"
HIRE, HIRE_ROLE = "hire", "hire_role"        # the Upwork-style hiring app

# Catalog category → short tab label for the Hire app.
_HIRE_TABS = {"office": "Office", "warriors": "Warriors", "fantasy": "Fantasy",
              "critters": "Critters"}

# Phone body geometry (centred on screen each frame).
PW, PH = 312, 600
LCD_MARGIN_X = 30
LCD_TOP = 74
LCD_H = 312
FONT = 16
LINE_H = 19
SPINNER = "|/-\\"

# Nokia palette.
BODY = pr.Color(46, 56, 82, 255)          # classic blue-grey shell
BODY_EDGE = pr.Color(28, 34, 52, 255)
KEY = pr.Color(70, 80, 108, 255)
KEY_EDGE = pr.Color(30, 36, 54, 255)
BRAND = pr.Color(210, 218, 235, 255)
LCD_BG = pr.Color(150, 178, 112, 255)     # pale phosphor green
LCD_EDGE = pr.Color(66, 84, 50, 255)
INK = pr.Color(36, 50, 28, 255)           # dark text
INK_DIM = pr.Color(86, 104, 62, 255)      # status / secondary text
HILITE = pr.Color(46, 62, 30, 255)        # selected row fill (text drawn in LCD_BG)


def _short(text: str, n: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


class PhonePanel:
    def __init__(self, link, coordinator, contacts_getter, inbox, taskboard=None,
                 hire=None) -> None:
        self.link = link                  # CompanyLink (agent 1:1 chat)
        self.coord = coordinator          # CoordinatorLink (co-founder)
        self._contacts = contacts_getter  # () -> list[Character]
        self.inbox = inbox                # Inbox: messages that come TO the CEO
        self.board = taskboard            # tasks.TaskBoard (the To-Do app), optional
        self.hire = hire                  # hire bridge (catalog/cash/unlock/hire), optional
        self._hire_tabs: list[str] = []   # catalog categories, lazily filled
        self._hire_tab = 0                # current category tab in the Hire app
        self._hire_item = None            # catalog model picked, awaiting a role choice
        self._hire_flash = ""             # transient "Office full" / "Hired!" message
        self._home_msg = ""               # transient note on the home screen (locked feature)
        self.open = False
        self.screen = HOME
        self.sel = 0                      # cursor in menus / contact / inbox list
        self.input = ""
        self.agent = None                 # Character for AGENT / CALL screens
        self._msg = None                  # InboxMessage being read on MESSAGE
        self._scroll = 0                  # lines scrolled up from the bottom
        self._list_top = 0                # first visible row index in a long list

        # Co-founder thread (kept in memory; each line is (kind, text)).
        self._cf_log: list[tuple[str, str]] = []
        self._cf_waiting = False
        self._cf_voice = voice.pick_voice(COFOUNDER_NAME)   # Robin reads replies aloud too

        # Agent 1:1 thread streaming state (mirrors ChatPanel).
        self._waiting = False
        self._partial = ""
        self._step = ""
        self._wait_start = 0.0
        self._voice_name = None

        # Push-to-talk: hold R2 / Left-Ctrl to speak to the agent or co-founder
        # (mic → Gemini transcription → sent as your message). No-op without a mic.
        self.voice_in = voice.VoiceInput(GEMINI_MODEL)
        self._voice_status = ""

        # Call screen state.
        self._call_t0 = 0.0
        self._call_live = False

    # --- lifecycle ---------------------------------------------------------

    def open_panel(self) -> None:
        self.open = True
        self.screen = HOME
        self.sel = 0
        self.input = ""
        while pr.get_char_pressed() > 0:   # swallow the key that opened us
            pass

    def open_hire(self) -> None:
        """Open straight to the Hire app (used by the staffing-agency building)."""
        self.open_panel()
        self.screen, self.sel, self._list_top = HIRE, 0, 0
        self._hire_tab, self._hire_item, self._hire_flash = 0, None, ""

    # --- hire-app data helpers --------------------------------------------

    def _hire_cats(self) -> list[str]:
        """Catalog categories, in first-seen order (drives the Hire app tabs)."""
        if not self._hire_tabs and self.hire is not None:
            seen: list[str] = []
            for it in self.hire.catalog():
                c = it.get("category", "office")
                if c not in seen:
                    seen.append(c)
            self._hire_tabs = seen
        return self._hire_tabs

    def _hire_rows(self) -> list[dict]:
        """Catalog items in the current tab's category."""
        cats = self._hire_cats()
        if not cats:
            return []
        cat = cats[self._hire_tab % len(cats)]
        return [it for it in self.hire.catalog() if it.get("category", "office") == cat]

    def _hire_locked(self, it: dict) -> bool:
        return bool(it.get("locked")) and it["id"] not in self.hire.unlocked()

    def _cofounder_ready(self) -> bool:
        """Robin is only your co-founder once you've won them over (the cofounder
        to-do, done at the Bean Scene Cafe). Until then the phone won't text them."""
        return self.board is not None and self.board.is_done("cofounder")

    def close(self) -> None:
        voice.stop_speaking()
        self.voice_in.cancel()
        self._voice_status = ""
        self.open = False
        self.agent = None
        self.input = ""
        self._call_live = False

    @property
    def active_agent_id(self):
        """Backend id of the agent this phone currently owns a reply for, so the
        game's busy-reconciler doesn't steal it. None unless mid agent chat."""
        if self.screen == AGENT and self.agent is not None:
            return self.agent.backend_id
        return None

    @property
    def capturing(self) -> bool:
        """True on the message-composing screens, where the keyboard types into the
        text field — so the game must NOT treat keys like N as a close toggle here."""
        return self.open and self.screen in (COFOUNDER, AGENT)

    # --- geometry (shared by update + draw) -------------------------------

    def _geom(self):
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        bx, by = (sw - PW) // 2, (sh - PH) // 2
        lx, ly = bx + LCD_MARGIN_X, by + LCD_TOP
        lw, lh = PW - 2 * LCD_MARGIN_X, LCD_H
        return bx, by, lx, ly, lw, lh

    def _rows(self, count: int):
        """y of each selectable row in a short (always-fits) list, and row height."""
        _, _, lx, ly, lw, lh = self._geom()
        top = ly + 26                      # below the LCD status strip
        rh = LINE_H + 4
        return [top + i * rh for i in range(count)], rh, lx, lw

    def _list_view(self, n: int):
        """A scrolling viewport for a long list: keeps self.sel on screen.

        Returns (start, ys, rh, lx, lw) where ys[i] is the y of the i-th *visible*
        row, which shows list item (start + i)."""
        _, _, lx, ly, lw, lh = self._geom()
        top = ly + 26
        rh = LINE_H + 4
        visible = max(1, (ly + lh - 4 - top) // rh)
        if self.sel < self._list_top:
            self._list_top = self.sel
        elif self.sel >= self._list_top + visible:
            self._list_top = self.sel - visible + 1
        self._list_top = max(0, min(self._list_top, max(0, n - visible)))
        ys = [top + i * rh for i in range(min(visible, n - self._list_top))]
        return self._list_top, ys, rh, lx, lw

    # --- per-frame: input + polling ---------------------------------------

    def update(self) -> None:
        if not self.open:
            return

        # Keep the co-founder run advancing no matter which screen we're on, so a
        # long delegation finishing while you browse Contacts still lands.
        self._pump_cofounder()

        wheel = pr.get_mouse_wheel_move()
        if wheel:
            self._scroll = max(0, self._scroll + int(wheel * 2))

        if self.screen == HOME:
            self._update_home()
        elif self.screen == HIRE:
            self._update_hire()
        elif self.screen == HIRE_ROLE:
            self._update_hire_role()
        elif self.screen == TODO:
            self._update_todo()
        elif self.screen == INBOX:
            self._update_inbox()
        elif self.screen == MESSAGE:
            self._update_message()
        elif self.screen == CONTACTS:
            self._update_contacts()
        elif self.screen == COFOUNDER:
            self._update_thread(is_cofounder=True)
        elif self.screen == AGENT:
            self._update_thread(is_cofounder=False)
        elif self.screen == CALL:
            self._update_call()

    # menu / list navigation helpers
    def _nav(self, n: int) -> None:
        if n <= 0:
            return
        if pr.is_key_pressed(pr.KEY_UP) or gamepad.pressed(gamepad.DPAD_UP):
            self.sel = (self.sel - 1) % n
        if pr.is_key_pressed(pr.KEY_DOWN) or gamepad.pressed(gamepad.DPAD_DOWN):
            self.sel = (self.sel + 1) % n

    def _enter(self) -> bool:
        return pr.is_key_pressed(pr.KEY_ENTER) or gamepad.pressed(gamepad.CROSS)

    def _back(self) -> bool:
        return pr.is_key_pressed(pr.KEY_ESCAPE) or gamepad.pressed(gamepad.CIRCLE)

    def _update_home(self) -> None:
        items = 6
        self._nav(items)
        # Mouse: click a row to pick it.
        ys, rh, lx, lw = self._rows(items)
        m = pr.get_mouse_position()
        clicked = pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)
        for i, ry in enumerate(ys):
            if clicked and pr.check_collision_point_rec(m, pr.Rectangle(lx, ry, lw, rh)):
                self.sel = i
                self._activate_home()
                return
        if self._enter():
            self._activate_home()
        elif self._back():
            self.close()

    def _activate_home(self) -> None:
        self._home_msg = ""
        if self.sel == 0:
            self.screen, self.sel, self._scroll, self._list_top = INBOX, 0, 0, 0
        elif self.sel == 1:
            if not self._cofounder_ready():      # Robin hasn't agreed to join yet
                self._home_msg = (f"You haven't won {COFOUNDER_NAME} over yet — "
                                  "pitch them at the Bean Scene Cafe.")
                return
            self.screen, self._scroll, self.input = COFOUNDER, 0, ""
        elif self.sel == 2:
            self.screen, self.sel, self._scroll, self._list_top = CONTACTS, 0, 0, 0
        elif self.sel == 3:
            self.screen, self.sel, self._list_top = HIRE, 0, 0
            self._hire_tab, self._hire_item, self._hire_flash = 0, None, ""
        elif self.sel == 4:
            self.screen, self.sel, self._list_top = TODO, 0, 0
        else:
            self.close()

    def _update_inbox(self) -> None:
        msgs = self.inbox.messages()
        n = len(msgs)
        self._nav(n)
        start, ys, rh, lx, lw = self._list_view(n)
        m = pr.get_mouse_position()
        clicked = pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)
        for i, ry in enumerate(ys):
            if clicked and pr.check_collision_point_rec(m, pr.Rectangle(lx, ry, lw, rh)):
                self.sel = start + i
                self._open_message(msgs[start + i])
                return
        if self._back():
            self.screen, self.sel = HOME, 0
            return
        if n and self._enter():
            self.sel = min(self.sel, n - 1)
            self._open_message(msgs[self.sel])

    def _update_todo(self) -> None:
        n = len(tasks.TASKS)
        self._nav(n)
        if self._back():
            self.screen, self.sel = HOME, 4

    # --- Hire app (browse models → pick a role → hire) --------------------

    def _update_hire(self) -> None:
        if self.hire is None or self._back():
            self.screen, self.sel = HOME, 3
            return
        cats = self._hire_cats()
        if cats:                                   # Left/Right switch category tabs
            if pr.is_key_pressed(pr.KEY_LEFT) or gamepad.pressed(gamepad.DPAD_LEFT):
                self._hire_tab = (self._hire_tab - 1) % len(cats)
                self.sel, self._list_top, self._hire_flash = 0, 0, ""
            if pr.is_key_pressed(pr.KEY_RIGHT) or gamepad.pressed(gamepad.DPAD_RIGHT):
                self._hire_tab = (self._hire_tab + 1) % len(cats)
                self.sel, self._list_top, self._hire_flash = 0, 0, ""
        rows = self._hire_rows()
        n = len(rows)
        self._nav(n)
        if n and self._enter():
            self.sel = min(self.sel, n - 1)
            self._pick_hire(rows[self.sel])

    def _pick_hire(self, it: dict) -> None:
        """Enter on a catalog row: buy it if locked, else go choose a role."""
        if self._hire_locked(it):
            if self.hire.cash() < it.get("unlock", 0):
                self._hire_flash = "Can't afford unlock"
            elif self.hire.unlock(it):
                self._hire_flash = f"Unlocked {_short(it['name'], 16)}"
            return
        if not self.hire.can_hire():
            self._hire_flash = "Office is full — lease more space"
            return
        if self.hire.cash() < it.get("price", 0):
            self._hire_flash = "Not enough cash"
            return
        self._hire_item = it
        self.screen, self.sel, self._list_top, self._hire_flash = HIRE_ROLE, 0, 0, ""

    def _update_hire_role(self) -> None:
        if self.hire is None or self._back():
            self.screen, self.sel, self._list_top = HIRE, 0, 0
            return
        roles = self.hire.roles()
        n = len(roles)
        self._nav(n)
        if n and self._enter():
            self.sel = min(self.sel, n - 1)
            role = roles[self.sel]
            if self.hire.hire(self._hire_item, role):
                name = _short(self._hire_item["name"], 14)
                self._hire_item = None
                self.screen, self.sel, self._list_top = HIRE, 0, 0
                self._hire_flash = f"Hired a {_short(role, 18)}"
            else:
                self._hire_flash = "Hire failed (full / broke)"

    def _open_message(self, msg) -> None:
        self.inbox.mark_read(msg)
        self._msg = msg
        self.screen, self._scroll = MESSAGE, 0

    def _update_message(self) -> None:
        if self._back():
            self.screen, self._scroll = INBOX, 0
            return
        # Reply (only for agent messages we can route): left soft-key / R.
        m = self._msg
        if m is not None and m.agent_id and pr.is_key_pressed(pr.KEY_R):
            for c in self._contacts():
                if c.backend_id == m.agent_id:
                    self._open_agent(c)
                    return

    def _update_contacts(self) -> None:
        people = self._contacts()
        n = len(people)
        self._nav(n)
        start, ys, rh, lx, lw = self._list_view(n)
        m = pr.get_mouse_position()
        clicked = pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)
        for i, ry in enumerate(ys):
            if clicked and pr.check_collision_point_rec(m, pr.Rectangle(lx, ry, lw, rh)):
                self.sel = start + i
        if self._back():
            self.screen, self.sel = HOME, 1
            return
        if n == 0:
            return
        self.sel = min(self.sel, n - 1)
        if pr.is_key_pressed(pr.KEY_C):
            self._start_call(people[self.sel])
        elif self._enter():
            self._open_agent(people[self.sel])

    def _open_agent(self, agent) -> None:
        self.agent = agent
        self.screen, self.input, self._scroll = AGENT, "", 0
        self._partial, self._step = "", ""
        self._waiting = self.link.is_busy(agent.backend_id)
        self._wait_start = pr.get_time() if self._waiting else 0.0
        self._voice_name = voice.pick_voice(agent.backend_id)

    # --- text threads (co-founder + agent) --------------------------------

    def _update_thread(self, *, is_cofounder: bool) -> None:
        if self._back():
            self.voice_in.cancel()
            voice.stop_speaking()
            self._voice_status = ""
            self.screen = HOME if is_cofounder else CONTACTS
            return

        waiting = self._cf_waiting if is_cofounder else self._waiting
        if not is_cofounder:
            self._pump_agent()             # poll streaming reply for the agent
            waiting = self._waiting

        self._update_voice_thread(is_cofounder, waiting)   # push-to-talk (hold R2 / Ctrl)

        if not waiting:                    # typing only when not mid-reply
            ch = pr.get_char_pressed()
            while ch > 0:
                if 32 <= ch < 127 and len(self.input) < 300:
                    self.input += chr(ch)
                ch = pr.get_char_pressed()
            bs = pr.is_key_pressed(pr.KEY_BACKSPACE)
            if hasattr(pr, "is_key_pressed_repeat"):
                bs = bs or pr.is_key_pressed_repeat(pr.KEY_BACKSPACE)
            if bs and self.input:
                self.input = self.input[:-1]

        if pr.is_key_pressed(pr.KEY_ENTER) and self.input.strip() and not waiting:
            if is_cofounder:
                self._cf_send(self.input)
            else:
                self._agent_send(self.input)

    def _update_voice_thread(self, is_cofounder: bool, waiting: bool) -> None:
        """Hold R2 / Left-Ctrl to record; release to transcribe and auto-send — so
        you can talk to the agent/co-founder instead of typing (great on a pad)."""
        held = (gamepad.down(gamepad.R2) or pr.is_key_down(pr.KEY_LEFT_CONTROL)
                or pr.is_key_down(pr.KEY_RIGHT_CONTROL))
        if held and not waiting and not self.voice_in.recording and not self.voice_in.transcribing:
            self.voice_in.begin()
            if self.voice_in.recording:
                self._voice_status = "listening…"
        elif self.voice_in.recording and (not held or waiting):
            self.voice_in.end()
            self._voice_status = "transcribing…" if self.voice_in.transcribing else ""

        result = self.voice_in.poll()
        if result is None:
            return
        self._voice_status = ""
        if result.startswith("[voice error"):
            self._voice_status = result
        elif result.strip() and not waiting:
            if is_cofounder:
                self._cf_send(result)
            else:
                self._agent_send(result)

    def _cf_send(self, text: str) -> None:
        text = text.strip()
        self._cf_log.append(("you", text))
        self.input, self._scroll = "", 0
        if self.coord.send(text):
            self._cf_waiting = True
        else:
            self._cf_log.append(("sys", f"⚠ {self.coord.error or 'co-founder busy'}"))

    def _pump_cofounder(self) -> None:
        if not self._cf_waiting:
            return
        for ev in self.coord.poll():
            if ev.kind == "plan":
                tasks = ev.payload or []
                self._cf_log.append(("sys", f"Coordinating {len(tasks)} task(s)…"))
                for t in tasks:
                    self._cf_log.append(("sys", f"• {t.role}: {_short(t.description)}"))
            elif ev.kind == "task_done":
                r = ev.payload
                self._cf_log.append(("sys", f"✓ {getattr(r, 'role', 'agent')} done"))
            elif ev.kind == "error":
                self._cf_log.append(("sys", f"⚠ {ev.payload}"))
        reply = self.coord.poll_reply()
        if reply is not None:
            self._cf_waiting = False
            kind = "sys" if reply.startswith("[error") else "cf"
            self._cf_log.append((kind, reply))
            self._scroll = 0
            if kind == "cf":                       # Robin replies aloud, like the agents
                voice.speak(reply, self._cf_voice)

    def _agent_send(self, text: str) -> None:
        text = text.strip()
        # Texting a Recruiter/HR "hire an engineer" hires for you right here, instead
        # of going to the model — the same bridge the office chat uses.
        if (self.hire is not None and self.agent is not None
                and self.hire.is_hr(self.agent.role)):
            ack = self.hire.hire_by_text(text)
            if ack is not None:
                if self.agent.backend_id:
                    self.link.store.add_message(self.agent.backend_id, "human", text)
                    self.link.store.add_message(self.agent.backend_id, "ai", ack)
                self.input, self._scroll = "", 0
                voice.speak(ack, self._voice_name)
                return
        if self.link.send(self.agent.backend_id, text):
            self.input, self._scroll = "", 0
            self._partial, self._step = "", ""
            self._waiting = True
            self._wait_start = pr.get_time()
            self.agent.status = "working"

    def _pump_agent(self) -> None:
        if not self._waiting:
            return
        step = self.link.poll_steps(self.agent.backend_id)
        if step:
            self._step = step
        for tok in self.link.poll_tokens(self.agent.backend_id):
            self._partial = "" if tok is None else self._partial + tok
        reply = self.link.poll_reply(self.agent.backend_id)
        if reply is not None:
            self._waiting = False
            self._partial = self._step = ""
            self.agent.status = "idle"
            self._scroll = 0
            if not reply.startswith("[error"):
                voice.speak(reply, self._voice_name)

    # --- call -------------------------------------------------------------

    def _start_call(self, agent) -> None:
        self.agent = agent
        self.screen = CALL
        self._call_t0 = pr.get_time()
        self._call_live = False
        self._voice_name = voice.pick_voice(agent.backend_id)

    def _update_call(self) -> None:
        if self._back():
            voice.stop_speaking()
            self.screen = CONTACTS
            return
        # Ring for a beat, then "connect" and let the agent greet you aloud.
        if not self._call_live and pr.get_time() - self._call_t0 > 1.4:
            self._call_live = True
            voice.speak(f"Hi, it's {self.agent.name}. What do you need?", self._voice_name)

    # --- draw -------------------------------------------------------------

    def draw(self) -> None:
        if not self.open:
            return
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pr.draw_rectangle(0, 0, sw, sh, pr.Color(0, 0, 0, 150))
        bx, by, lx, ly, lw, lh = self._geom()
        self._draw_body(bx, by)

        pr.draw_rectangle(lx - 4, ly - 4, lw + 8, lh + 8, LCD_EDGE)
        pr.draw_rectangle(lx, ly, lw, lh, LCD_BG)
        pr.begin_scissor_mode(lx, ly, lw, lh)
        if self.screen == HOME:
            self._draw_home(lx, ly, lw, lh)
        elif self.screen == HIRE:
            self._draw_hire(lx, ly, lw, lh)
        elif self.screen == HIRE_ROLE:
            self._draw_hire_role(lx, ly, lw, lh)
        elif self.screen == TODO:
            self._draw_todo(lx, ly, lw, lh)
        elif self.screen == INBOX:
            self._draw_inbox(lx, ly, lw, lh)
        elif self.screen == MESSAGE:
            self._draw_message(lx, ly, lw, lh)
        elif self.screen == CONTACTS:
            self._draw_contacts(lx, ly, lw, lh)
        elif self.screen in (COFOUNDER, AGENT):
            self._draw_thread(lx, ly, lw, lh, is_cofounder=self.screen == COFOUNDER)
        elif self.screen == CALL:
            self._draw_call(lx, ly, lw, lh)
        pr.end_scissor_mode()

        self._draw_softkeys(bx, by, ly + lh)

    def _draw_body(self, bx: int, by: int) -> None:
        outer = pr.Rectangle(bx - 4, by - 4, PW + 8, PH + 8)
        pr.draw_rectangle_rounded(outer, 0.18, 10, BODY_EDGE)
        pr.draw_rectangle_rounded(pr.Rectangle(bx, by, PW, PH), 0.16, 10, BODY)
        # Earpiece slit + brand.
        pr.draw_rectangle_rounded(pr.Rectangle(bx + PW / 2 - 26, by + 18, 52, 7), 1.0, 6,
                                  BODY_EDGE)
        bw = pr.measure_text("NOKIA", 20)
        pr.draw_text("NOKIA", int(bx + (PW - bw) / 2), by + 34, 20, BRAND)
        # Keypad: nav cluster + a 3x4 grid of keys (decorative; navigation is by
        # arrows/Enter, mouse, or controller).
        nav_cy = by + LCD_TOP + LCD_H + 34
        cx = bx + PW // 2
        pr.draw_circle(cx, nav_cy, 26, KEY_EDGE)
        pr.draw_circle(cx, nav_cy, 22, KEY)
        pr.draw_circle(cx, nav_cy, 8, BODY)
        labels = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]
        sub = ["", "abc", "def", "ghi", "jkl", "mno", "pqrs", "tuv", "wxyz", "", "+", ""]
        gw, gh = 70, 26
        gap_x, gap_y = 10, 6
        grid_w = 3 * gw + 2 * gap_x
        gx0 = cx - grid_w // 2
        gy0 = nav_cy + 28
        for i, lab in enumerate(labels):
            r, c = divmod(i, 3)
            kx = gx0 + c * (gw + gap_x)
            ky = gy0 + r * (gh + gap_y)
            pr.draw_rectangle_rounded(pr.Rectangle(kx, ky, gw, gh), 0.5, 6, KEY)
            pr.draw_rectangle_rounded_lines(pr.Rectangle(kx, ky, gw, gh), 0.5, 6, KEY_EDGE)
            pr.draw_text(lab, kx + 8, ky + 6, 18, BRAND)
            if sub[i]:
                sw_ = pr.measure_text(sub[i], 10)
                pr.draw_text(sub[i], kx + gw - sw_ - 7, ky + 11, 10, pr.Color(150, 158, 180, 255))

    def _draw_status_strip(self, lx: int, ly: int, lw: int, title: str) -> None:
        # Signal bars (left), title (centre-ish), battery (right) — classic.
        for i in range(4):
            bh = 4 + i * 3
            pr.draw_rectangle(lx + 4 + i * 5, ly + 14 - bh, 3, bh, INK)
        pr.draw_text(title, lx + 30, ly + 4, 13, INK)
        bx = lx + lw - 22
        pr.draw_rectangle_lines(bx, ly + 4, 18, 9, INK)
        pr.draw_rectangle(bx + 18, ly + 6, 2, 5, INK)
        pr.draw_rectangle(bx + 2, ly + 6, 11, 5, INK)
        pr.draw_line(lx, ly + 18, lx + lw, ly + 18, INK_DIM)

    def _draw_home(self, lx, ly, lw, lh) -> None:
        self._draw_status_strip(lx, ly, lw, "Menu")
        unread = self.inbox.unread()
        todo_hint = ""
        if self.board is not None:
            d, t = self.board.progress()
            todo_hint = f"{d}/{t} done"
        hire_hint = ""
        if self.hire is not None:
            hire_hint = "browse talent" if self.hire.can_hire() else "office full"
        # Robin isn't your co-founder until you win them over — say so until then.
        cf_hint = f"text {COFOUNDER_NAME}" if self._cofounder_ready() else f"win {COFOUNDER_NAME} over first"
        items = [("Inbox", f"{unread} new" if unread else "no new messages"),
                 ("New Message", cf_hint),
                 ("Contacts", "message / call an agent"),
                 ("Hire", hire_hint),
                 ("To-Do", todo_hint),
                 ("Close", "")]
        ys, rh, _, _ = self._rows(len(items))
        for i, (label, hint) in enumerate(items):
            self._draw_row(lx, ys[i], lw, rh, label, hint, i == self.sel)
        if self._home_msg:                       # locked-feature nudge, wrapped
            my = ys[-1] + rh + 6
            for line in _wrap(self._home_msg, lw - 12, 13)[:3]:
                pr.draw_text(line, lx + 6, my, 13, INK_DIM)
                my += 16

    def _draw_inbox(self, lx, ly, lw, lh) -> None:
        msgs = self.inbox.messages()
        self._draw_status_strip(lx, ly, lw, f"Inbox ({self.inbox.unread()})")
        if not msgs:
            pr.draw_text("No messages yet.", lx + 8, ly + 30, FONT, INK_DIM)
            return
        start, ys, rh, _, _ = self._list_view(len(msgs))
        for i, ry in enumerate(ys):
            msg = msgs[start + i]
            selected = (start + i) == self.sel
            if selected:
                pr.draw_rectangle(lx + 2, ry - 2, lw - 4, rh, HILITE)
            fg = LCD_BG if selected else INK
            dim = LCD_BG if selected else INK_DIM
            if not msg.read:
                pr.draw_circle(lx + 9, ry + 8, 3, fg)
            pr.draw_text(msg.sender, lx + 18, ry - 1, 14, fg)
            tag = "NPC" if msg.kind == "npc" else ("✓" if msg.kind == "agent" else "")
            if tag:
                tw = pr.measure_text(tag, 10)
                pr.draw_text(tag, lx + lw - tw - 8, ry + 1, 10, dim)

    def _draw_message(self, lx, ly, lw, lh) -> None:
        m = self._msg
        if m is None:
            return
        self._draw_status_strip(lx, ly, lw, "Message")
        y = ly + 24
        pr.draw_text(m.sender, lx + 8, y, FONT, INK)
        kindlabel = {"npc": "business", "agent": "your team",
                     "cofounder": COFOUNDER_NAME}.get(m.kind, "system")
        kw = pr.measure_text(kindlabel, 11)
        pr.draw_text(kindlabel, lx + lw - kw - 8, y + 3, 11, INK_DIM)
        y += LINE_H + 2
        pr.draw_line(lx, y, lx + lw, y, INK_DIM)
        y += 4
        body_top = y
        input_y = ly + lh - 4
        lines = _wrap(m.body, lw - 14, FONT)
        visible = max(1, (input_y - body_top) // LINE_H)
        self._scroll = min(self._scroll, max(0, len(lines) - visible))
        start = self._scroll
        for line in lines[start:start + visible]:
            pr.draw_text(line, lx + 7, y, FONT, INK)
            y += LINE_H

    def _draw_contacts(self, lx, ly, lw, lh) -> None:
        people = self._contacts()
        self._draw_status_strip(lx, ly, lw, f"Contacts ({len(people)})")
        if not people:
            pr.draw_text("No agents hired yet.", lx + 8, ly + 30, FONT, INK_DIM)
            return
        start, ys, rh, _, _ = self._list_view(len(people))
        for i, ry in enumerate(ys):
            p = people[start + i]
            self._draw_row(lx, ry, lw, rh, p.name, p.role, (start + i) == self.sel)

    def _draw_todo(self, lx, ly, lw, lh) -> None:
        if self.board is None:
            self._draw_status_strip(lx, ly, lw, "To-Do")
            pr.draw_text("No to-do list yet.", lx + 8, ly + 30, FONT, INK_DIM)
            return
        d, t = self.board.progress()
        self._draw_status_strip(lx, ly, lw, f"To-Do {d}/{t}")
        cur = self.board.current()
        start, ys, rh, _, _ = self._list_view(len(tasks.TASKS))
        for i, ry in enumerate(ys):
            tk = tasks.TASKS[start + i]
            done = self.board.is_done(tk.key)
            is_cur = cur is not None and tk.key == cur.key
            selected = (start + i) == self.sel
            if selected:
                pr.draw_rectangle(lx + 2, ry - 2, lw - 4, rh, HILITE)
            fg = LCD_BG if selected else INK
            box = pr.Rectangle(lx + 8, ry + 2, 12, 12)
            if done:
                pr.draw_rectangle_rec(box, fg)
            else:
                pr.draw_rectangle_lines_ex(box, 1, fg)
            if is_cur and not done:                 # "you are here" marker
                pr.draw_text(">", lx + 24, ry, 14, fg)
            title = tk.title                        # trim to fit the narrow LCD
            while title and pr.measure_text(title, 14) > lw - 42:
                title = title[:-1]
            pr.draw_text(title, lx + 34, ry, 14, fg)

    def _draw_hire(self, lx, ly, lw, lh) -> None:
        if self.hire is None:
            self._draw_status_strip(lx, ly, lw, "Hire")
            pr.draw_text("Hiring unavailable.", lx + 8, ly + 30, FONT, INK_DIM)
            return
        cats = self._hire_cats()
        cat = cats[self._hire_tab % len(cats)] if cats else "office"
        label = _HIRE_TABS.get(cat, cat.title())
        self._draw_status_strip(lx, ly, lw, f"Hire ‹{label}›")
        rows = self._hire_rows()
        if not rows:
            pr.draw_text("Nobody available.", lx + 8, ly + 30, FONT, INK_DIM)
            return
        # leave a line at the bottom for the flash / tab hint
        foot = self._hire_flash or "◄ ► category"
        pr.draw_text(_short(foot, 30), lx + 6, ly + lh - 16, 12, INK_DIM)
        start, ys, rh, _, _ = self._list_view_h(len(rows), bottom_pad=20)
        for i, ry in enumerate(ys):
            it = rows[start + i]
            selected = (start + i) == self.sel
            locked = self._hire_locked(it)
            if selected:
                pr.draw_rectangle(lx + 2, ry - 2, lw - 4, rh, HILITE)
            fg = LCD_BG if selected else INK
            dim = LCD_BG if selected else INK_DIM
            nx = lx + 8
            if locked:                              # padlock glyph before the name
                pr.draw_rectangle(nx, int(ry) + 6, 9, 7, fg)
                pr.draw_ring(pr.Vector2(nx + 4, int(ry) + 6), 2, 4, 180, 360, 12, fg)
                nx += 14
            name = it["name"]
            while name and pr.measure_text(name, 14) > lw - 80:
                name = name[:-1]
            pr.draw_text(name, nx, ry, 14, fg)
            cost = it.get("unlock", 0) if locked else it.get("price", 0)
            tag = f"${cost:,}"
            tw = pr.measure_text(tag, 12)
            pr.draw_text(tag, lx + lw - tw - 8, ry + 1, 12, dim)

    def _draw_hire_role(self, lx, ly, lw, lh) -> None:
        item = self._hire_item
        who = _short(item["name"], 18) if item else "—"
        self._draw_status_strip(lx, ly, lw, "Pick a role")
        pr.draw_text(f"Hire {who} as:", lx + 8, ly + 24, 13, INK_DIM)
        roles = self.hire.roles() if self.hire else []
        if self._hire_flash:
            pr.draw_text(_short(self._hire_flash, 30), lx + 6, ly + lh - 16, 12, INK_DIM)
        start, ys, rh, _, _ = self._list_view_h(len(roles), top=ly + 44, bottom_pad=20)
        for i, ry in enumerate(ys):
            role = roles[start + i]
            selected = (start + i) == self.sel
            if selected:
                pr.draw_rectangle(lx + 2, ry - 2, lw - 4, rh, HILITE)
            fg = LCD_BG if selected else INK
            r = role
            while r and pr.measure_text(r, 14) > lw - 20:
                r = r[:-1]
            pr.draw_text(r, lx + 8, ry, 14, fg)

    def _list_view_h(self, n: int, top: int | None = None, bottom_pad: int = 0):
        """Like _list_view but lets the Hire screens reserve a top header and a
        bottom footer line. Returns (start, ys, rh, lx, lw)."""
        _, _, lx, ly, lw, lh = self._geom()
        top = (ly + 26) if top is None else top
        rh = LINE_H + 4
        visible = max(1, (ly + lh - 4 - bottom_pad - top) // rh)
        if self.sel < self._list_top:
            self._list_top = self.sel
        elif self.sel >= self._list_top + visible:
            self._list_top = self.sel - visible + 1
        self._list_top = max(0, min(self._list_top, max(0, n - visible)))
        ys = [top + i * rh for i in range(min(visible, n - self._list_top))]
        return self._list_top, ys, rh, lx, lw

    def _draw_row(self, lx, y, lw, rh, label, hint, selected) -> None:
        if selected:
            pr.draw_rectangle(lx + 2, y - 2, lw - 4, rh, HILITE)
        fg = LCD_BG if selected else INK
        pr.draw_text(label, lx + 8, y, FONT, fg)
        if hint:
            hw = pr.measure_text(hint, 11)
            pr.draw_text(hint, lx + lw - hw - 8, y + 3, 11,
                         LCD_BG if selected else INK_DIM)

    def _thread_lines(self, is_cofounder: bool, max_w: int):
        out: list[tuple[pr.Color, str]] = []
        if is_cofounder:
            for kind, text in self._cf_log:
                color = {"you": INK, "cf": INK, "sys": INK_DIM}[kind]
                who = "You" if kind == "you" else (COFOUNDER_NAME if kind == "cf" else "")
                body = f"{who}: {text}" if who else text
                for wl in _wrap(body, max_w, FONT):
                    out.append((color, wl))
            if self._cf_waiting:
                out.append((INK_DIM, self._wait_line(COFOUNDER_NAME)))
        else:
            for m in self.link.history(self.agent.backend_id):
                who = "You" if m.role == "human" else self.agent.name
                for wl in _wrap(f"{who}: {m.content}", max_w, FONT):
                    out.append((INK, wl))
            if self._waiting:
                if self._partial:
                    for wl in _wrap(f"{self.agent.name}: {self._partial}", max_w, FONT):
                        out.append((INK, wl))
                else:
                    out.append((INK_DIM, self._wait_line(self.agent.name)))
        return out

    def _wait_line(self, name: str) -> str:
        spin = SPINNER[int(pr.get_time() * 8) % len(SPINNER)]
        verb = self._step or "thinking"
        return f"{spin} {name} is {verb}…"

    def _draw_thread(self, lx, ly, lw, lh, *, is_cofounder: bool) -> None:
        title = COFOUNDER_NAME if is_cofounder else self.agent.name
        self._draw_status_strip(lx, ly, lw, title)
        body_top = ly + 24
        input_y = ly + lh - 22
        max_w = lw - 14
        lines = self._thread_lines(is_cofounder, max_w)
        visible = max(1, (input_y - body_top) // LINE_H)
        max_scroll = max(0, len(lines) - visible)
        self._scroll = min(self._scroll, max_scroll)
        end = len(lines) - self._scroll
        ty = body_top
        for color, line in lines[max(0, end - visible):end]:
            pr.draw_text(line, lx + 7, ty, FONT, color)
            ty += LINE_H
        # Input line at the bottom of the LCD.
        pr.draw_line(lx, input_y - 4, lx + lw, input_y - 4, INK_DIM)
        waiting = self._cf_waiting if is_cofounder else self._waiting
        if self.voice_in.recording or self.voice_in.transcribing or self._voice_status:
            label = self._voice_status or "listening…"
            mark = "●" if self.voice_in.recording else "…"
            pr.draw_text(f"{mark} {label}", lx + 7, input_y, FONT,
                         pr.Color(150, 40, 30, 255) if self.voice_in.recording else INK_DIM)
        elif waiting:
            pr.draw_text("…sending, you can step away", lx + 7, input_y, 12, INK_DIM)
        else:
            caret = "_" if (pr.get_time() % 1.0) < 0.5 else " "
            hint = "> " + self.input + caret if self.input else "> type, or hold Ctrl/R2 to talk"
            col = INK if self.input else INK_DIM
            pr.draw_text(hint, lx + 7, input_y, FONT, col)

    def _draw_call(self, lx, ly, lw, lh) -> None:
        self._draw_status_strip(lx, ly, lw, "Call")
        cx = lx + lw // 2
        name = self.agent.name
        nw = pr.measure_text(name, 22)
        pr.draw_text(name, cx - nw // 2, ly + 70, 22, INK)
        rw = pr.measure_text(self.agent.role, 14)
        pr.draw_text(self.agent.role, cx - rw // 2, ly + 98, 14, INK_DIM)
        elapsed = int(pr.get_time() - self._call_t0)
        if self._call_live:
            status = f"In call   {elapsed // 60:02d}:{elapsed % 60:02d}"
        else:
            dots = "." * (1 + int(pr.get_time() * 2) % 3)
            status = f"Calling{dots}"
        sw_ = pr.measure_text(status, 16)
        pr.draw_text(status, cx - sw_ // 2, ly + 150, 16, INK)
        # A little handset glyph.
        pr.draw_circle(cx, ly + lh - 60, 18, HILITE)
        pr.draw_text("☎", cx - 8, ly + lh - 72, 20, LCD_BG)

    def _draw_softkeys(self, bx, by, lcd_bottom) -> None:
        if self.screen == HOME:
            left, right = "Select", "Exit"
        elif self.screen == INBOX:
            left, right = "Open", "Back"
        elif self.screen == MESSAGE:
            left = "Reply" if (self._msg and self._msg.agent_id) else ""
            right = "Back"
        elif self.screen == CONTACTS:
            left, right = "Message", "Back"
        elif self.screen == TODO:
            left, right = "", "Back"
        elif self.screen == HIRE:
            left, right = ("Unlock" if (self._hire_rows() and
                           self._hire_locked(self._hire_rows()[min(self.sel, len(self._hire_rows()) - 1)]))
                           else "Pick"), "Back"
        elif self.screen == HIRE_ROLE:
            left, right = "Hire", "Back"
        elif self.screen == CALL:
            left, right = "", "End"
        else:
            left, right = "Send", "Back"
        y = lcd_bottom + 6
        if left:
            pr.draw_text(left, bx + LCD_MARGIN_X, y, 13, BRAND)
        if right:
            rw = pr.measure_text(right, 13)
            pr.draw_text(right, bx + PW - LCD_MARGIN_X - rw, y, 13, BRAND)
        center = ""                                 # extra key affordance, if any
        if self.screen == CONTACTS:
            center = "C = Call"
        elif self.screen == MESSAGE and self._msg and self._msg.agent_id:
            center = "R = Reply"
        if center:
            hw = pr.measure_text(center, 12)
            pr.draw_text(center, bx + (PW - hw) // 2, y, 12, pr.Color(160, 168, 190, 255))

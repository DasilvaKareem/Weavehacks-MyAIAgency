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
        # The terminal NEVER locks input. A directive is queued + run on a background
        # pump; _thinking just drives the indicator, _queued shows how many are behind
        # the running one, and _term_gen_seen tracks when a turn finished so we re-read.
        self._thinking = False
        self._queued = 0
        self._term_gen_seen = 0
        self._lines: list[tuple[pr.Color, str]] = []   # cached wrapped transcript
        self._scroll = 0                # lines scrolled up from the bottom (0 = newest)
        self._status = ""               # transient line in the input row (voice, etc.)
        self._step = ""                 # live tool-loop activity ("using X")
        self._partial = ""              # streamed answer so far this turn
        self._wait_start = 0.0
        # Background firehose: how many fire-and-forget tasks are running. The count
        # comes from CompanyLink's cached, off-thread poll (cheap to read every
        # frame); when it drops, dispatched results have landed so we re-read the log.
        self._bg_tasks = 0
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
        # Tab cycles the views: chat | files | sessions.
        self.screen = "chat"            # "chat" | "files" | "sessions"
        # Sessions view: the saved conversations + the highlighted row.
        self._sessions: list = []       # cached [{id,title,active}] (newest first)
        self._ssel = 0                  # selected session index
        # OPS view (24/7 operations): scheduled jobs, run history, approval queue.
        self._ops_view = "jobs"         # "jobs" | "activity" | "approvals"
        self._ops_rows: list = []       # cached rows for the current sub-view
        self._osel = 0                  # selected ops row
        self._ops_at = 0.0              # last load time (drives ~1s auto-refresh)
        self._ops_pending = 0           # pending-approval count (shown on the chip)
        self._oname: dict = {}          # agent_id -> name cache (for job rows)
        # MONITOR view (W&B Weave): the live AI-workforce quality leaderboard.
        self._mon_rows: list = []       # cached leaderboard rows (best-first)
        self._mon_sel = 0               # selected agent row
        self._mon_at = 0.0              # last poll time (drives ~1.5s refresh)
        self._mon_url_rect = None       # click-to-open W&B dashboard hit-box
        self._mon_row_rects: list = []  # (row_index, rect) hit-boxes for click-select
        self._files: list = []          # cached FileRow list
        self._fsel = 0                  # selected file index
        self._flisttop = 0              # first visible row (list scroll)
        self._ftex: dict = {}           # disk_path -> Texture2D | None (image previews)
        self._row_rects: list = []      # (index, Rectangle) for click-to-select
        self._tab_rects: dict = {}      # "chat"/"files" -> Rectangle (clickable tabs)
        # @-mention picker: type '@' in the chat input to tag a hired employee. The
        # picked teammate(s) ride along to the run as hard delegation targets.
        self._employees: list = []      # cached [{id,name,role}] for autocomplete
        self._mpick: list = []          # current filtered matches (set in update)
        self._msel = 0                  # highlighted match
        self._mention_hide = None       # (at_index, query) the user Esc-dismissed
        self._mention_was_active = False  # rising-edge flag (reload roster on open)
        self._mention_rects: list = []  # (index, Rectangle) click-to-pick (set in draw)

    # --- lifecycle ---------------------------------------------------------

    def open_panel(self, company_name: str = "") -> None:
        self.open = True
        self.input = ""
        self._company = (company_name or "company").strip().lower().replace(" ", "-")[:18]
        self._thinking = self.link.terminal_busy()
        self._queued = self.link.terminal_queued()
        self._term_gen_seen = self.link.terminal_generation()   # don't refresh on open
        self._wait_start = pr.get_time() if self._thinking else 0.0
        self._step = self._partial = self._status = ""
        self._link_rects = []
        self._hire = None
        self._open_url = None
        self.screen = "chat"
        self._mention_hide = None
        self._mention_was_active = False
        self._msel = 0
        self._load_employees()              # roster for the @-mention picker
        while pr.get_char_pressed() > 0:    # drop the 'e' that opened the panel
            pass
        self._refresh()

    def close(self) -> None:
        self.voice.cancel()
        voice.stop_speaking()
        pr.set_mouse_cursor(pr.MOUSE_CURSOR_DEFAULT)    # drop any link-hover pointer
        self.open = False
        self.input = ""
        self._thinking = False
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
        # Fire-and-forget: the directive is echoed + queued instantly and the input is
        # cleared so you can immediately type the next one. No "busy" lock, ever.
        self.link.terminal_send(text, mentions=self._collect_mentions(text))
        self.input = ""
        self._open_url = None
        self._mention_hide = None
        self._scroll = 0
        self._refresh()                 # show the just-echoed directive right away

    # --- @-mention picker --------------------------------------------------

    def _load_employees(self) -> None:
        try:
            self._employees = self.link.terminal_employees()
        except Exception:
            self._employees = []

    def _mention_tail(self):
        """If the cursor is mid-@mention, return (at_index, query); else None.

        A mention is "in progress" from the last '@' to the end while that tail has
        no whitespace yet (typing a space — or picking, which appends one — ends it),
        and it hasn't been Esc-dismissed for this exact tail."""
        i = self.input.rfind("@")
        if i < 0:
            return None
        tail = self.input[i + 1:]
        if any(c.isspace() for c in tail):
            return None
        if self._mention_hide == (i, tail):
            return None
        return (i, tail)

    def _mention_matches(self, query: str) -> list:
        """Hired employees whose name or role matches the typed query (max 8)."""
        q = query.strip().lower()
        out = [e for e in self._employees
               if not q or q in e["name"].lower() or q in e["role"].lower()]
        return out[:8]

    def _insert_mention(self, at_index: int, emp: dict) -> None:
        """Replace the in-progress '@query' with the chosen '@Full Name ' token."""
        self.input = self.input[:at_index] + "@" + emp["name"] + " "
        if len(self.input) > MAX_INPUT:
            self.input = self.input[:MAX_INPUT]
        self._mention_hide = None
        self._msel = 0
        while pr.get_char_pressed() > 0:    # swallow the Enter/Tab that picked it
            pass

    def _collect_mentions(self, text: str) -> list:
        """Employees the CEO @-tagged in `text` (matched on '@Full Name')."""
        low = text.lower()
        return [e for e in self._employees if ("@" + e["name"].lower()) in low]

    def _update_mention_picker(self) -> bool:
        """Drive the autocomplete popup; returns True when it consumed this frame's
        input (so the caller skips normal Tab/Enter/Esc handling)."""
        st = self._mention_tail()
        active = st is not None
        if active and not self._mention_was_active:
            self._load_employees()          # refresh roster when a mention begins
        self._mention_was_active = active
        if not active:
            self._mpick = []
            return False
        self._mpick = self._mention_matches(st[1])
        if not self._mpick:
            return False
        self._msel = max(0, min(self._msel, len(self._mpick) - 1))

        if pr.is_key_pressed(pr.KEY_DOWN):
            self._msel = (self._msel + 1) % len(self._mpick)
            return True
        if pr.is_key_pressed(pr.KEY_UP):
            self._msel = (self._msel - 1) % len(self._mpick)
            return True
        if pr.is_key_pressed(pr.KEY_ENTER) or pr.is_key_pressed(pr.KEY_TAB):
            self._insert_mention(st[0], self._mpick[self._msel])
            return True
        if pr.is_key_pressed(pr.KEY_ESCAPE) or gamepad.pressed(gamepad.CIRCLE):
            self._mention_hide = st         # dismiss the popup, keep typing/terminal
            return True
        if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
            mp = pr.get_mouse_position()
            for idx, rect in self._mention_rects:
                if pr.check_collision_point_rec(mp, rect) and idx < len(self._mpick):
                    self._insert_mention(st[0], self._mpick[idx])
                    return True
        return False

    def _poll_background(self) -> None:
        """Read the cached firehose backlog (CompanyLink refreshes it off-thread, so
        this never blocks). When it drops, dispatched results have been appended to
        the log — re-read it so they appear without re-sending."""
        prev = self._bg_tasks
        self._bg_tasks = self.link.terminal_pending_tasks()
        if self._bg_tasks < prev and not self._thinking and self._scroll == 0:
            self._refresh()

    def _poll_terminal(self) -> None:
        """Stream the in-flight turn's activity and re-read the log when a turn
        finishes — all without ever blocking the input. Runs every frame."""
        step = self.link.poll_steps(TERMINAL_ID)
        if step:
            self._step = step
        for tok in self.link.poll_tokens(TERMINAL_ID):
            self._partial = "" if tok is None else self._partial + tok
        gen = self.link.terminal_generation()
        if gen != self._term_gen_seen:                  # a turn just completed
            self._term_gen_seen = gen
            self._step = self._partial = ""
            if self._scroll == 0:
                self._refresh()
            hist = self.link.terminal_history()
            last = hist[-1].content if hist else ""
            if last and not last.startswith(("[error", "[done")):
                voice.speak(last, None)                 # read the reply back
                clean = re.sub(r"\[dead link removed[^\]]*\]", "", last)
                urls = _URL_RE.findall(clean)
                self._open_url = urls[-1].rstrip(_URL_TRIM) if urls else None
        was = self._thinking
        self._thinking = self.link.terminal_busy()
        self._queued = self.link.terminal_queued()
        if self._thinking and not was:
            self._wait_start = pr.get_time()

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
        order = ("chat", "files", "sessions", "ops", "monitor")
        self.screen = order[(order.index(self.screen) + 1) % len(order)]
        if self.screen == "files":
            self._load_files()
        elif self.screen == "sessions":
            self._load_sessions()
        elif self.screen == "ops":
            self._load_ops()
        elif self.screen == "monitor":
            self._load_monitor()
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

    # --- sessions ----------------------------------------------------------

    def _load_sessions(self) -> None:
        self._sessions = self.link.terminal_sessions()
        # Keep the highlight on the active conversation when (re)opening the view.
        self._ssel = next((i for i, s in enumerate(self._sessions) if s.get("active")), 0)

    def _open_session(self) -> None:
        """Switch to the highlighted session and drop back to its chat."""
        if not self._sessions:
            return
        sid = self._sessions[self._ssel]["id"]
        if self.link.terminal_switch_session(sid):
            self.screen = "chat"
            self._open_url = None
            self._refresh()
        else:
            self._status = "can't switch — finish the running order first"

    def _new_session(self) -> None:
        if self.link.terminal_new_session() is None:
            self._status = "can't start a new chat while one's running"
            self._load_sessions()
            return
        self.screen = "chat"
        self._open_url = None
        self._refresh()

    def _delete_session(self) -> None:
        if not self._sessions:
            return
        sid = self._sessions[self._ssel]["id"]
        if self.link.terminal_delete_session(sid):
            self._load_sessions()
            self._ssel = min(self._ssel, max(0, len(self._sessions) - 1))
            self._refresh()         # active session may have changed
        else:
            self._status = "can't delete while a turn is running"

    def _update_sessions(self) -> None:
        n = len(self._sessions)
        if n:
            if pr.is_key_pressed(pr.KEY_DOWN):
                self._ssel = min(n - 1, self._ssel + 1)
            if pr.is_key_pressed(pr.KEY_UP):
                self._ssel = max(0, self._ssel - 1)
        if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
            mp = pr.get_mouse_position()
            for i, rect in self._row_rects:
                if pr.check_collision_point_rec(mp, rect):
                    if i == self._ssel:             # click the highlighted row = open it
                        self._open_session()
                    else:
                        self._ssel = i
                    break
        if pr.is_key_pressed(pr.KEY_ENTER):
            self._open_session()
        if pr.is_key_pressed(pr.KEY_N):
            self._new_session()
        if pr.is_key_pressed(pr.KEY_DELETE) or pr.is_key_pressed(pr.KEY_BACKSPACE):
            self._delete_session()
        if pr.is_key_pressed(pr.KEY_ESCAPE) or gamepad.pressed(gamepad.CIRCLE):
            self.screen = "chat"                    # Esc backs out to chat, not close

    # --- 24/7 operations ---------------------------------------------------

    OPS_VIEWS = ("jobs", "activity", "approvals")

    def _agent_name(self, agent_id: str) -> str:
        if agent_id not in self._oname:
            self._oname[agent_id] = self.link.terminal_agent_name(agent_id)
        return self._oname[agent_id]

    def _load_ops(self) -> None:
        v = self._ops_view
        try:
            if v == "jobs":
                self._ops_rows = self.link.terminal_jobs()
            elif v == "approvals":
                self._ops_rows = self.link.terminal_approvals()
            else:
                self._ops_rows = self.link.terminal_runs(40)
            # Pending-approval count for the chip badge, from any sub-view.
            self._ops_pending = (len(self._ops_rows) if v == "approvals"
                                 else len(self.link.terminal_approvals()))
        except Exception as exc:
            self._ops_rows = []
            self._status = str(exc)[:70]
        if self._osel >= len(self._ops_rows):
            self._osel = max(0, len(self._ops_rows) - 1)
        self._ops_at = pr.get_time()

    def _set_ops_view(self, step: int) -> None:
        i = (self.OPS_VIEWS.index(self._ops_view) + step) % len(self.OPS_VIEWS)
        self._ops_view = self.OPS_VIEWS[i]
        self._osel = 0
        self._status = ""
        self._load_ops()

    def _update_ops(self) -> None:
        if pr.get_time() - self._ops_at > 1.0:      # live refresh as the worker runs
            self._load_ops()
        if pr.is_key_pressed(pr.KEY_RIGHT):
            self._set_ops_view(1); return
        if pr.is_key_pressed(pr.KEY_LEFT):
            self._set_ops_view(-1); return
        n = len(self._ops_rows)
        if n:
            if pr.is_key_pressed(pr.KEY_DOWN):
                self._osel = min(n - 1, self._osel + 1)
            if pr.is_key_pressed(pr.KEY_UP):
                self._osel = max(0, self._osel - 1)
        if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
            mp = pr.get_mouse_position()
            for i, rect in self._row_rects:
                if pr.check_collision_point_rec(mp, rect):
                    self._osel = i
                    break
        if pr.is_key_pressed(pr.KEY_R):
            self._load_ops()
        row = self._ops_rows[self._osel] if 0 <= self._osel < n else None
        if row is not None:
            self._ops_action(row)
        if pr.is_key_pressed(pr.KEY_ESCAPE) or gamepad.pressed(gamepad.CIRCLE):
            self.screen = "chat"

    def _ops_action(self, row) -> None:
        v = self._ops_view
        if v == "jobs":
            if pr.is_key_pressed(pr.KEY_T):
                self.link.terminal_toggle_job(row.id, not row.enabled)
                self._status = f"{row.name}: {'paused' if row.enabled else 'enabled'}"
                self._load_ops()
            if pr.is_key_pressed(pr.KEY_ENTER):
                if self.link.terminal_run_job_now(row.id):
                    self._status = f"queued '{row.name}' to run now"
        elif v == "approvals":
            if pr.is_key_pressed(pr.KEY_Y):
                self.link.terminal_decide_approval(row.id, "approved")
                self._status = "approved"; self._load_ops()
            if pr.is_key_pressed(pr.KEY_N):
                self.link.terminal_decide_approval(row.id, "rejected")
                self._status = "rejected"; self._load_ops()
        elif v == "activity":
            if pr.is_key_pressed(pr.KEY_ENTER) and getattr(row, "status", "") in ("done", "error"):
                self.link.terminal_retry_run(row.id)
                self._status = "retry queued"; self._load_ops()

    # --- monitor (W&B Weave live quality) ----------------------------------

    def _load_monitor(self) -> None:
        self.link.refresh_leaderboard()             # kick an off-thread Weave fetch
        self._mon_rows = self.link.poll_leaderboard()
        if self._mon_sel >= len(self._mon_rows):
            self._mon_sel = max(0, len(self._mon_rows) - 1)
        self._mon_at = pr.get_time()

    def _update_monitor(self) -> None:
        if pr.get_time() - self._mon_at > 1.5:      # cheap: cached, refreshes off-thread
            self._load_monitor()
        n = len(self._mon_rows)
        if n:
            if pr.is_key_pressed(pr.KEY_DOWN):
                self._mon_sel = min(n - 1, self._mon_sel + 1)
            if pr.is_key_pressed(pr.KEY_UP):
                self._mon_sel = max(0, self._mon_sel - 1)
        if pr.is_key_pressed(pr.KEY_R):
            self.link.refresh_leaderboard()
            self._status = "refreshing from Weave..."
        # F: hand the selected crashing agent to the Observability Engineer to fix.
        if pr.is_key_pressed(pr.KEY_F) and 0 <= self._mon_sel < n:
            row = self._mon_rows[self._mon_sel]
            if (row.get("error_rate", 0) or 0) > 0 and not self.link.observability_fix_pending():
                self.link.ask_observability_fix(row)
                self._status = f"asking the Observability Engineer to fix {row.get('name','?')}…"
        # Click an agent row to select it (→ its diagnosis pane updates).
        if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
            mp = pr.get_mouse_position()
            for idx, rect in self._mon_row_rects:
                if pr.check_collision_point_rec(mp, rect):
                    self._mon_sel = idx
                    break
        # Click (or press O) the dashboard link to open the full W&B Weave UI.
        open_dash = pr.is_key_pressed(pr.KEY_O)
        if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT) and self._mon_url_rect:
            if pr.check_collision_point_rec(pr.get_mouse_position(), self._mon_url_rect):
                open_dash = True
        if open_dash:
            url = self.link.weave_dashboard_url()
            _open_externally(url)
            self._status = f"opening {url[:46]}..."
        if pr.is_key_pressed(pr.KEY_ESCAPE) or gamepad.pressed(gamepad.CIRCLE):
            self.screen = "chat"

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

        # The @-mention popup owns the keyboard while it's up: it claims Tab/Enter/
        # arrows/Esc/click for picking, so check it before the screen + send handlers.
        if self.screen == "chat" and self._update_mention_picker():
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
                        elif name == "sessions":
                            self._load_sessions()
                        elif name == "ops":
                            self._load_ops()
                        elif name == "monitor":
                            self._load_monitor()
                    return
        if self.screen == "files":
            self._update_files()
            return
        if self.screen == "sessions":
            self._update_sessions()
            return
        if self.screen == "ops":
            self._update_ops()
            return
        if self.screen == "monitor":
            self._update_monitor()
            return

        self._poll_background()
        self._poll_terminal()           # stream + pick up finished turns; never blocks

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

        # A hire the terminal proposed: surface it for the CEO to confirm (Y) or
        # cancel (N/Esc) right here. This is the one genuine modal — a hire spends
        # money — so it does pause typing until decided.
        if self._hire is None:
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

        self._update_voice()                        # push-to-talk
        if self.voice.recording or self.voice.transcribing:
            return                                  # only voice capture pauses typing

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
            elif m.content.startswith("[done · "):
                # A background task landed: header line in cyan so completions pop,
                # the summary under it in dim green.
                for i, wl in enumerate(_wrap(m.content, body_w, FONT)):
                    lines.append((CYAN if i == 0 else GREEN_DIM, wl))
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
        q = f"   (+{self._queued} queued)" if self._queued else ""
        return f"{spin} {verb}...{clock}{q}"

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
        status = "[ ONLINE ]" if not self._thinking else "[ THINKING ]"
        sc = CYAN if not self._thinking else AMBER
        sw_t = pr.measure_text(status, 18)
        pr.draw_text(status, x + w - PAD - sw_t, y + 20, 18, sc)
        sub = "delegates your orders to the team and gets it done"
        pr.draw_text(sub, x + PAD, y + 46, 15, GREEN_DIM)
        if self._bg_tasks:                          # firehose busy — work in flight
            spin = SPINNER[int(pr.get_time() * 8) % len(SPINNER)]
            pr.draw_text(f"{spin} {self._bg_tasks} working in background",
                         x + PAD + pr.measure_text(sub, 15) + 20, y + 46, 15, CYAN)
        self._draw_tabs(x, y + 42, w)
        pr.draw_line(x + PAD, y + 70, x + w - PAD, y + 70, GREEN_FAINT)

        # The Files browser / Sessions list replace the chat body/input when active.
        if self.screen == "files":
            self._draw_files(x, y, w, h)
            self._draw_scanlines(x, y, w, h)
            return
        if self.screen == "sessions":
            self._draw_sessions(x, y, w, h)
            self._draw_scanlines(x, y, w, h)
            return
        if self.screen == "ops":
            self._draw_ops(x, y, w, h)
            self._draw_scanlines(x, y, w, h)
            return
        if self.screen == "monitor":
            self._draw_monitor(x, y, w, h)
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

        # While a turn runs (and pinned to newest): stream the answer above the input
        # — you can keep typing the next order underneath it the whole time.
        if self._thinking and self._scroll == 0:
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
        else:
            # Prompt is ALWAYS live — type the next order even while a turn runs.
            caret = "█" if (pr.get_time() % 1.0) < 0.5 else " "
            prompt = self._prompt()
            pr.draw_text(prompt, x + PAD, input_y, FONT, GREEN_DIM)
            pw = pr.measure_text(prompt, FONT)
            pr.draw_text(self.input + caret, x + PAD + pw, input_y, FONT, AMBER)

        # @-mention autocomplete pops up just above the input line.
        self._draw_mention_picker(x, w, input_y)

        talk = "hold Ctrl talk" if voice.available() else "(mic off)"
        hint = f"ENTER send   ·   @ tag a teammate   ·   {talk}   ·   TAB tabs   ·   ESC close"
        pr.draw_text(hint, x + PAD, y + h - 26, 14, GREEN_DIM)

        self._draw_scanlines(x, y, w, h)

    def _draw_mention_picker(self, x: int, w: int, input_y: int) -> None:
        """Floating list of hired employees matching the in-progress '@query'.
        Records click hit-boxes into self._mention_rects (consumed by update)."""
        self._mention_rects = []
        st = self._mention_tail()
        if st is None:
            return
        matches = self._mention_matches(st[1])
        if not matches:
            return
        self._msel = max(0, min(self._msel, len(matches) - 1))
        row_h = 24
        bx = x + PAD
        bw = min(440, w - 2 * PAD)
        bh = len(matches) * row_h + 10
        by = input_y - 16 - bh
        pr.draw_rectangle(bx, by, bw, bh, pr.Color(10, 26, 16, 252))
        pr.draw_rectangle_lines_ex(pr.Rectangle(bx, by, bw, bh), 1, GREEN_DIM)
        for i, e in enumerate(matches):
            ry = by + 5 + i * row_h
            rect = pr.Rectangle(bx + 2, ry - 2, bw - 4, row_h)
            self._mention_rects.append((i, rect))
            sel = (i == self._msel)
            if sel:
                pr.draw_rectangle_rec(rect, pr.Color(22, 54, 34, 255))
            name = f"@{e['name']}"
            pr.draw_text(name, bx + 8, ry, 17, GREEN if sel else GREEN_DIM)
            role = str(e.get("role") or "")
            rw = pr.measure_text(role, 14)
            pr.draw_text(role, bx + bw - 10 - rw, ry + 2, 14, GREEN_FAINT)

    # --- files browser draw ------------------------------------------------

    def _draw_tabs(self, x: int, y: int, w: int) -> None:
        """Clickable [ CHAT ] [ FILES ] [ SESSIONS ] tabs in the header (right-aligned)."""
        self._tab_rects = {}
        tx = x + w - PAD
        for name, lab in (("monitor", "MONITOR"), ("ops", "24/7 OPS"),
                          ("sessions", "SESSIONS"), ("files", "FILES"), ("chat", "CHAT")):
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

    # --- sessions list draw ------------------------------------------------

    def _draw_sessions(self, x: int, y: int, w: int, h: int) -> None:
        top = y + 84
        bottom = y + h - 46
        list_x = x + PAD
        list_w = w - 2 * PAD
        self._row_rects = []
        sessions = self._sessions

        pr.draw_text("CONVERSATIONS", list_x, top, 15, GREEN_DIM)
        list_top = top + 28

        if not sessions:
            pr.draw_text("No saved chats yet.", list_x, list_top, FONT, GREEN_FAINT)
        else:
            rows = max(1, (bottom - list_top) // LINE_H)
            top_row = max(0, min(self._ssel - rows + 1, len(sessions) - rows)) \
                if self._ssel >= rows else 0
            ty = list_top
            for i in range(top_row, min(len(sessions), top_row + rows)):
                s = sessions[i]
                rect = pr.Rectangle(list_x - 4, ty - 2, list_w + 8, LINE_H)
                self._row_rects.append((i, rect))
                sel = (i == self._ssel)
                if sel:
                    pr.draw_rectangle_rec(rect, pr.Color(22, 54, 34, 255))
                marker = "> " if sel else "  "
                tag = "  [active]" if s.get("active") else ""
                label = f"{marker}{i + 1}. {s['title']}"
                while label and pr.measure_text(label + tag, 18) > list_w - 12:
                    label = label[:-2]
                pr.draw_text(label, list_x, ty, 18, GREEN if sel else GREEN_DIM)
                if tag:
                    lw = pr.measure_text(label, 18)
                    pr.draw_text(tag, list_x + lw, ty, 18, CYAN)
                ty += LINE_H

        footer = self._status or ("↑↓ select   ·   Enter open   ·   N new chat   ·   "
                                  "Del delete   ·   Tab chat   ·   Esc back")
        pr.draw_text(footer, list_x, y + h - 26, 14,
                     AMBER if self._status else GREEN_DIM)

    # --- 24/7 operations draw ----------------------------------------------

    @staticmethod
    def _ts(value) -> str:
        """Trim an ISO timestamp to 'MM-DD HH:MM' for compact rows."""
        s = str(value or "")
        return s[5:16].replace("T", " ") if len(s) >= 16 else s

    def _ops_label(self, row) -> str:
        v = self._ops_view
        if v == "jobs":
            state = "ON " if row.enabled else "off"
            return (f"[{state}] {row.name}  ·  {self._agent_name(row.agent_id)}  ·  "
                    f"{row.schedule_type} {row.schedule_value}  ·  next {self._ts(row.next_run_at)}")
        if v == "approvals":
            return f"[{row.action_class}] {row.tool_name}  ·  run {row.run_id}"
        return f"[{row.status}] {self._ts(row.created_at)}  ·  {row.agent_name}  ·  {row.source_type}"

    def _ops_detail(self, row) -> str:
        v = self._ops_view
        if v == "jobs":
            return row.instruction
        if v == "approvals":
            return getattr(row, "tool_args", "") or ""
        return row.report or row.error or row.instruction or ""

    def _draw_ops(self, x: int, y: int, w: int, h: int) -> None:
        list_x = x + PAD
        list_w = w - 2 * PAD
        self._row_rects = []

        # Sub-view chips: JOBS | ACTIVITY | APPROVALS (click or ←/→).
        cx = list_x
        empties = {"jobs": "No scheduled jobs yet — tell the terminal e.g. "
                           "'every weekday 9am have the Researcher post an AI-news digest'.",
                   "activity": "No autonomous runs yet.",
                   "approvals": "Nothing waiting for approval."}
        for v in self.OPS_VIEWS:
            lab = v.upper()
            n = f" ({self._ops_pending})" if (v == "approvals" and self._ops_pending) else ""
            txt = f" {lab}{n} "
            tw = pr.measure_text(txt, 15) + 6
            rect = pr.Rectangle(cx, y + 84, tw, 24)
            on = (v == self._ops_view)
            pr.draw_rectangle_rec(rect, pr.Color(22, 54, 34, 255) if on else SCREEN)
            pr.draw_rectangle_lines_ex(rect, 1, GREEN if on else GREEN_FAINT)
            pr.draw_text(txt, int(cx + 3), y + 88, 15, GREEN if on else GREEN_DIM)
            if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT) and \
                    pr.check_collision_point_rec(pr.get_mouse_position(), rect) and not on:
                self._ops_view = v
                self._osel = 0
                self._load_ops()
            cx += tw + 8

        top = y + 122
        bottom = y + h - 92
        rows = self._ops_rows

        if not rows:
            for i, wl in enumerate(_wrap(empties[self._ops_view], list_w, FONT)):
                pr.draw_text(wl, list_x, top + i * LINE_H, FONT, GREEN_FAINT)
        else:
            fit = max(1, (bottom - top) // LINE_H)
            top_row = 0 if self._osel < fit else \
                min(self._osel - fit + 1, max(0, len(rows) - fit))
            ty = top
            for i in range(top_row, min(len(rows), top_row + fit)):
                sel = (i == self._osel)
                rect = pr.Rectangle(list_x - 4, ty - 2, list_w + 8, LINE_H)
                self._row_rects.append((i, rect))
                if sel:
                    pr.draw_rectangle_rec(rect, pr.Color(22, 54, 34, 255))
                label = self._ops_label(rows[i])
                while label and pr.measure_text(label, 17) > list_w - 12:
                    label = label[:-2]
                pr.draw_text(label, list_x, ty, 17, GREEN if sel else GREEN_DIM)
                ty += LINE_H
            # Detail of the highlighted row, wrapped under the list.
            detail = self._ops_detail(rows[self._osel]) if 0 <= self._osel < len(rows) else ""
            if detail:
                pr.draw_line(list_x, bottom + 2, x + w - PAD, bottom + 2, GREEN_FAINT)
                for i, wl in enumerate(_wrap(detail, list_w, 15)[:2]):
                    pr.draw_text(wl, list_x, bottom + 8 + i * 20, 15, GREEN_DIM)

        actions = {
            "jobs": "←→ section · ↑↓ select · Enter run now · T on/off · R refresh · Tab/Esc back",
            "activity": "←→ section · ↑↓ select · Enter retry · R refresh · Tab/Esc back",
            "approvals": "←→ section · ↑↓ select · Y approve · N reject · R refresh · Tab/Esc back",
        }
        footer = self._status or actions[self._ops_view]
        pr.draw_text(footer, list_x, y + h - 26, 14, AMBER if self._status else GREEN_DIM)

    # --- monitor draw (W&B Weave live quality) -----------------------------

    def _draw_monitor(self, x: int, y: int, w: int, h: int) -> None:
        list_x = x + PAD
        list_w = w - 2 * PAD
        rows = self._mon_rows
        self._mon_url_rect = None

        pr.draw_text("AI WORKFORCE — LIVE QUALITY  (W&B Weave)", list_x, y + 84, 15, GREEN_DIM)

        if not self.link.weave_enabled():
            for i, wl in enumerate(_wrap(
                    "Weave tracing is off — set WANDB_API_KEY in .env to monitor your "
                    "agents' quality, cost and crashes here.", list_w, FONT)):
                pr.draw_text(wl, list_x, y + 120 + i * LINE_H, FONT, GREEN_FAINT)
            return

        if not rows:
            if self.link.leaderboard_pending():
                msg = "Connecting to W&B Weave — fetching live agent traces…"
                col = CYAN
            else:
                msg = ("No traces scored yet — chat with a hired agent (replies auto-score) "
                       "or run some work, then press R to refresh.")
                col = GREEN_FAINT
            for i, wl in enumerate(_wrap(msg, list_w, FONT)):
                pr.draw_text(wl, list_x, y + 120 + i * LINE_H, FONT, col)
        else:
            calls = sum(r.get("calls", 0) for r in rows)
            spend = sum(r.get("cost_per_call", 0.0) * r.get("calls", 0) for r in rows)
            scored = sum(r.get("replies_scored", 0) for r in rows)
            pr.draw_text(f"{len(rows)} agents · {calls} calls · ~${spend:.2f} spent · "
                         f"{scored} replies scored", list_x, y + 106, 14, CYAN)
            # column header
            hy = y + 132
            pr.draw_text("#  AGENT (role)", list_x, hy, 14, GREEN_FAINT)
            cols = [("score", 0.42), ("qual", 0.55), ("$/call", 0.66),
                    ("lat", 0.78), ("crash", 0.88), ("calls", 0.96)]
            for lab, frac in cols:
                pr.draw_text(lab, list_x + int(list_w * frac), hy, 14, GREEN_FAINT)
            top = hy + 24
            bottom = y + h - 70 - self._MON_DETAIL_H   # leave room for the diagnosis pane
            fit = max(1, (bottom - top) // LINE_H)
            self._mon_row_rects = []
            for vi, i in enumerate(range(0, min(len(rows), fit))):
                r = rows[i]
                ty = top + vi * LINE_H
                sel = (i == self._mon_sel)
                rect = pr.Rectangle(list_x - 4, ty - 2, list_w + 8, LINE_H)
                self._mon_row_rects.append((i, rect))
                if sel:
                    pr.draw_rectangle_rec(rect, pr.Color(22, 54, 34, 255))
                col = AMBER if i == 0 else (GREEN if sel else GREEN_DIM)
                name = f"{i+1}. {r.get('name','?')} ({r.get('role','?')})"
                while name and pr.measure_text(name, 16) > int(list_w * 0.40):
                    name = name[:-2]
                pr.draw_text(name, list_x, ty, 16, col)
                q = "—" if r.get("quality") is None else f"{r['quality']:.0f}"
                crash = f"{r.get('error_rate',0)*100:.0f}%"
                cells = [f"{r.get('score',0):.0f}", q, f"${r.get('cost_per_call',0):.4f}",
                         f"{r.get('avg_latency',0):.1f}s", crash, str(r.get("calls", 0))]
                for (lab, frac), val in zip(cols, cells):
                    c = RED if (lab == "crash" and r.get("error_rate", 0) > 0.1) else col
                    pr.draw_text(val, list_x + int(list_w * frac), ty, 16, c)

            # Diagnosis pane for the highlighted agent: WHY it crashed + a fix.
            if 0 <= self._mon_sel < len(rows):
                self._draw_mon_detail(rows[self._mon_sel], list_x, list_w,
                                      bottom + 8, y + h - 56)

        # dashboard link (clickable) + hints
        url = self.link.weave_dashboard_url()
        ly = y + h - 44
        tag = "Open full dashboard ↗ "
        pr.draw_text(tag, list_x, ly, 14, GREEN_DIM)
        ux = list_x + pr.measure_text(tag, 14)
        shown = url if len(url) <= 60 else url[:57] + "..."
        pr.draw_text(shown, ux, ly, 14, LINK)
        self._mon_url_rect = pr.Rectangle(ux, ly - 1, pr.measure_text(shown, 14), 18)
        footer = self._status or ("↑↓ / click select an agent to see crashes + a fix"
                                  "   ·   O open W&B   ·   R refresh   ·   Tab/Esc back")
        pr.draw_text(footer, list_x, y + h - 24, 14, AMBER if self._status else GREEN_DIM)

    # Height reserved under the leaderboard for the selected agent's diagnosis pane.
    _MON_DETAIL_H = 128

    def _draw_mon_detail(self, r: dict, x: int, w: int, top: int, bottom: int) -> None:
        """Why the selected agent is crashing (its real Weave error) + a concrete fix.

        Healthy agents show a one-line all-clear; crashing ones show the latest
        exception and a plain-English remedy. Press F to hand it to the Observability
        Engineer, who diagnoses with its tools and applies a real fix — its reply
        then shows here under the agent."""
        pr.draw_line(x, top, x + w, top, GREEN_FAINT)
        ty = top + 8
        name = f"{r.get('name','?')} · {r.get('role','?')}"
        err_rate = r.get("error_rate", 0) or 0
        head_col = RED if err_rate > 0.1 else GREEN
        pr.draw_text(name, x, ty, 16, head_col)
        ty += 22

        if err_rate <= 0:
            pr.draw_text("✓ No crashes on record — this agent is healthy.",
                         x, ty, 14, GREEN_DIM)
            return

        aid = r.get("agent_id", "")
        diag = self.link.diagnose_agent(aid)
        if diag is None:                                  # still fetching
            pr.draw_text("Reading this agent's failures from Weave…", x, ty, 14, CYAN)
            return
        fails = diag.get("failures") or []
        if fails:
            for wl in _wrap(f"⚠ Crash: {fails[0]['error']}", w, 14)[:2]:
                pr.draw_text(wl, x, ty, 14, RED)
                ty += 18
        else:
            pr.draw_text(f"{err_rate*100:.0f}% crash rate (no error detail in the last "
                         "400 traces).", x, ty, 14, GREEN_DIM)
            ty += 18

        # The Observability Engineer's verdict takes over once requested.
        fix_reply = self.link.poll_observability_fix()
        if self.link.observability_fix_pending():
            pr.draw_text("🔧 Observability Engineer is investigating & applying a fix…",
                         x, ty, 14, CYAN)
        elif fix_reply and fix_reply[0] == aid and fix_reply[1]:
            for wl in _wrap(f"🔧 Engineer: {fix_reply[1]}", w, 14)[:3]:
                pr.draw_text(wl, x, ty, 14, pr.Color(120, 255, 150, 255))
                ty += 18
        else:
            for wl in _wrap(f"Fix: {diag.get('fix','')}", w, 14)[:2]:
                pr.draw_text(wl, x, ty, 14, pr.Color(120, 255, 150, 255))
                ty += 18
            pr.draw_text("Press F to have the Observability Engineer fix it.",
                         x, ty, 13, GREEN_FAINT)

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

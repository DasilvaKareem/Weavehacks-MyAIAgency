"""In-game control center for the local always-on worker service."""
from __future__ import annotations

import pyray as pr
from datetime import timedelta
from zoneinfo import ZoneInfo

from backend import config
from backend.scheduling import initial_due, iso_utc, next_interval, utc_now

PANEL_W, PANEL_H = 940, 610
PAD, ROW_H = 18, 34
TABS = ("Jobs", "Heartbeat", "Approvals", "Activity", "Agents")
BG = pr.Color(18, 22, 32, 245)
BAR = pr.Color(88, 92, 175, 255)
ROW = pr.Color(28, 32, 44, 255)
ROW_SEL = pr.Color(52, 56, 88, 255)
META = pr.Color(155, 165, 190, 255)
GOOD = pr.Color(75, 190, 120, 255)
WARN = pr.Color(220, 165, 75, 255)
BAD = pr.Color(205, 90, 90, 255)
TEXT = pr.Color(225, 232, 245, 255)


def _btn(rect, label, base, enabled=True) -> bool:
    hover = enabled and pr.check_collision_point_rec(pr.get_mouse_position(), rect)
    col = base if enabled else pr.Color(70, 72, 82, 255)
    if hover:
        col = pr.Color(min(col.r + 25, 255), min(col.g + 25, 255),
                       min(col.b + 25, 255), 255)
    pr.draw_rectangle_rec(rect, col)
    pr.draw_rectangle_lines_ex(rect, 1, pr.Color(10, 12, 20, 255))
    tw = pr.measure_text(label, 15)
    pr.draw_text(label, int(rect.x + (rect.width - tw) / 2),
                 int(rect.y + 7), 15, pr.RAYWHITE)
    return hover and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)


def _clip(text: str, chars: int = 74) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= chars else flat[:chars - 1] + "..."


class JobsPanel:
    def __init__(self, store) -> None:
        self.store = store
        self.open = False
        self.tab = 0
        self.rows: list = []
        self.sel = 0
        self.scroll = 0
        self.form = None
        self.focus = 0
        self.flash = ""

    def open_panel(self) -> None:
        self.open = True
        self.tab = 0
        self.sel = self.scroll = 0
        self.form = None
        self.flash = ""
        self._refresh()
        while pr.get_char_pressed() > 0:
            pass

    def close(self) -> None:
        self.open = False
        self.form = None

    def _refresh(self) -> None:
        loaders = (
            self.store.list_jobs, self.store.list_heartbeats,
            self.store.list_approvals, self.store.list_runs,
            self.store.list_agents,
        )
        try:
            self.rows = list(loaders[self.tab]())
        except Exception as exc:
            self.rows, self.flash = [], str(exc)
        self.sel = min(self.sel, max(0, len(self.rows) - 1))

    def _set_tab(self, tab: int) -> None:
        self.tab = tab
        self.sel = self.scroll = 0
        self.form = None
        self.flash = ""
        self._refresh()

    def update(self) -> None:
        if not self.open:
            return
        if pr.is_key_pressed(pr.KEY_ESCAPE):
            if self.form:
                self.form = None
            else:
                self.close()
            return
        if self.form:
            self._update_form()
            return
        wheel = pr.get_mouse_wheel_move()
        if wheel:
            self.scroll = max(0, self.scroll - int(wheel))
        if pr.is_key_pressed(pr.KEY_DOWN) and self.rows:
            self.sel = min(len(self.rows) - 1, self.sel + 1)
        if pr.is_key_pressed(pr.KEY_UP) and self.rows:
            self.sel = max(0, self.sel - 1)

    def _update_form(self) -> None:
        fields = ("name", "instruction", "value")
        if pr.is_key_pressed(pr.KEY_TAB):
            self.focus = (self.focus + 1) % len(fields)
        key = fields[self.focus]
        ch = pr.get_char_pressed()
        while ch > 0:
            if 32 <= ch < 127 and len(self.form[key]) < 180:
                self.form[key] += chr(ch)
            ch = pr.get_char_pressed()
        if pr.is_key_pressed(pr.KEY_BACKSPACE) and self.form[key]:
            self.form[key] = self.form[key][:-1]

    def draw(self) -> None:
        if not self.open:
            return
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        x, y = (sw - PANEL_W) // 2, (sh - PANEL_H) // 2
        pr.draw_rectangle(0, 0, sw, sh, pr.Color(0, 0, 0, 145))
        pr.draw_rectangle(x, y, PANEL_W, PANEL_H, BG)
        pr.draw_rectangle(x, y, PANEL_W, 44, BAR)
        pr.draw_text("24/7 Operations", x + PAD, y + 11, 22, pr.RAYWHITE)
        if self.form:
            self._draw_form(x, y)
        else:
            self._draw_tabs(x, y)
            self._draw_rows(x, y)
            self._draw_actions(x, y)
        if self.flash:
            pr.draw_text(_clip(self.flash, 105), x + PAD, y + PANEL_H - 22, 14, WARN)

    def _draw_tabs(self, x: int, y: int) -> None:
        tx = x + PAD
        for i, label in enumerate(TABS):
            w = pr.measure_text(label, 16) + 22
            rect = pr.Rectangle(tx, y + 52, w, 28)
            pr.draw_rectangle_rec(rect, BAR if i == self.tab else ROW)
            pr.draw_text(label, tx + 11, y + 59, 16, TEXT)
            if pr.check_collision_point_rec(pr.get_mouse_position(), rect) \
                    and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
                self._set_tab(i)
            tx += w + 8

    def _draw_rows(self, x: int, y: int) -> None:
        top, bottom = y + 94, y + PANEL_H - 86
        fit = max(1, (bottom - top) // ROW_H)
        self.scroll = min(self.scroll, max(0, len(self.rows) - fit))
        if self.sel < self.scroll:
            self.scroll = self.sel
        if self.sel >= self.scroll + fit:
            self.scroll = self.sel - fit + 1
        if not self.rows:
            pr.draw_text("Nothing here yet.", x + PAD, top + 8, 18, META)
        mouse = pr.get_mouse_position()
        for off, row in enumerate(self.rows[self.scroll:self.scroll + fit]):
            idx, ry = self.scroll + off, top + off * ROW_H
            rect = pr.Rectangle(x + PAD, ry, PANEL_W - 2 * PAD, ROW_H - 4)
            pr.draw_rectangle_rec(rect, ROW_SEL if idx == self.sel else ROW)
            pr.draw_text(self._row_label(row), int(rect.x) + 9, ry + 7, 15, TEXT)
            if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT) \
                    and pr.check_collision_point_rec(mouse, rect):
                self.sel = idx
        row = self.rows[self.sel] if self.rows else None
        detail = self._detail(row) if row else ""
        if detail:
            pr.draw_text(_clip(detail, 112), x + PAD, bottom + 5, 14, META)

    def _row_label(self, row) -> str:
        if self.tab == 0:
            return f"[{'on' if row.enabled else 'off'}] {row.name} | {row.schedule_type}:{row.schedule_value} | next {row.next_run_at}"
        if self.tab == 1:
            return f"[{'on' if row.enabled else 'off'}] {row.name} | every {row.interval_seconds}s | next {row.next_run_at}"
        if self.tab == 2:
            return f"[{row.action_class}] {row.tool_name} | run {row.run_id}"
        if self.tab == 3:
            return f"[{row.status}] {row.created_at} | {row.agent_name} | {row.source_type}"
        return f"{row.name} | {row.role} | trust: {row.trust_tier}"

    def _detail(self, row) -> str:
        if self.tab in (0, 1):
            return row.instruction
        if self.tab == 2:
            return row.tool_args
        if self.tab == 3:
            return row.report or row.error or row.instruction
        return "supervised < standard < trusted; critical actions always pause for CEO approval"

    def _draw_actions(self, x: int, y: int) -> None:
        foot = y + PANEL_H - 54
        row = self.rows[self.sel] if self.rows else None
        bx = x + PAD
        if self.tab == 0:
            if _btn(pr.Rectangle(bx, foot, 105, 30), "Add Job", BAR):
                self._open_form("job")
            bx += 115
            if row and _btn(pr.Rectangle(bx, foot, 95, 30), "Run Now", GOOD):
                self.store.enqueue_manual_run(row.agent_id, row.instruction, iso_utc(utc_now()))
                self.flash = "Queued manual run."
            bx += 105
            if row and _btn(pr.Rectangle(bx, foot, 95, 30), "Toggle", WARN):
                self.store.set_job_enabled(row.id, not row.enabled); self._refresh()
        elif self.tab == 1:
            if _btn(pr.Rectangle(bx, foot, 135, 30), "Add Checklist", BAR):
                self._open_form("heartbeat")
            bx += 145
            if row and _btn(pr.Rectangle(bx, foot, 95, 30), "Toggle", WARN):
                self.store.set_heartbeat_enabled(row.id, not row.enabled); self._refresh()
        elif self.tab == 2 and row:
            if _btn(pr.Rectangle(bx, foot, 105, 30), "Approve", GOOD):
                self.store.decide_approval(row.id, "approved"); self._refresh()
            bx += 115
            if _btn(pr.Rectangle(bx, foot, 105, 30), "Reject", BAD):
                self.store.decide_approval(row.id, "rejected"); self._refresh()
        elif self.tab == 3 and row and row.status in {"done", "error"}:
            if _btn(pr.Rectangle(bx, foot, 105, 30), "Retry", WARN):
                self.store.retry_run(row.id); self._refresh()
        elif self.tab == 4 and row:
            if _btn(pr.Rectangle(bx, foot, 125, 30), "Cycle Trust", WARN):
                tiers = ("supervised", "standard", "trusted")
                self.store.set_trust_tier(row.id, tiers[(tiers.index(row.trust_tier) + 1) % 3])
                self._refresh()
        if _btn(pr.Rectangle(x + PANEL_W - PAD - 210, foot, 95, 30), "Refresh", BAR):
            self._refresh()
        if _btn(pr.Rectangle(x + PANEL_W - PAD - 105, foot, 95, 30), "Close", BAR):
            self.close()

    def _open_form(self, kind: str) -> None:
        agents = self.store.list_agents()
        if not agents:
            self.flash = "Hire an agent first."
            return
        self.form = {"kind": kind, "agent": agents[0].id, "agent_i": 0,
                     "schedule": "interval", "name": "", "instruction": "",
                     "value": "3600"}
        self.focus = 0

    def _draw_form(self, x: int, y: int) -> None:
        f = self.form
        title = "New Scheduled Job" if f["kind"] == "job" else "New Heartbeat Checklist Entry"
        pr.draw_text(title, x + PAD, y + 62, 22, TEXT)
        agents = self.store.list_agents()
        agent = agents[f["agent_i"] % len(agents)]
        f["agent"] = agent.id
        if _btn(pr.Rectangle(x + PAD, y + 102, 330, 32),
                f"Agent: {agent.name} ({agent.role})", BAR):
            f["agent_i"] = (f["agent_i"] + 1) % len(agents)
        if f["kind"] == "job" and _btn(pr.Rectangle(x + 368, y + 102, 190, 32),
                                       f"Type: {f['schedule']}", BAR):
            kinds = ("once", "interval", "cron")
            f["schedule"] = kinds[(kinds.index(f["schedule"]) + 1) % len(kinds)]
            local_hour = (utc_now() + timedelta(hours=1)).astimezone(
                ZoneInfo(config.DEFAULT_TIMEZONE)
            ).strftime("%Y-%m-%dT%H:%M")
            f["value"] = {"once": local_hour, "interval": "3600",
                          "cron": "0 9 * * *"}[f["schedule"]]
        labels = ("Name", "Instruction", "Value (local ISO / seconds / cron)")
        fields = ("name", "instruction", "value")
        for i, (label, key) in enumerate(zip(labels, fields)):
            fy = y + 166 + i * 78
            pr.draw_text(label, x + PAD, fy, 15, META)
            rect = pr.Rectangle(x + PAD, fy + 20, PANEL_W - 2 * PAD, 38)
            pr.draw_rectangle_rec(rect, ROW_SEL if self.focus == i else ROW)
            pr.draw_text(_clip(f[key], 104), int(rect.x) + 8, int(rect.y) + 10, 16, TEXT)
            if pr.check_collision_point_rec(pr.get_mouse_position(), rect) \
                    and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT):
                self.focus = i
        foot = y + PANEL_H - 64
        if _btn(pr.Rectangle(x + PAD, foot, 120, 34), "Create", GOOD):
            self._save_form()
        if _btn(pr.Rectangle(x + PAD + 132, foot, 120, 34), "Cancel", BAD):
            self.form = None

    def _save_form(self) -> None:
        f = self.form
        try:
            if not f["name"].strip() or not f["instruction"].strip():
                raise ValueError("Name and instruction are required.")
            if f["kind"] == "heartbeat":
                seconds = int(f["value"])
                self.store.create_heartbeat(
                    f["agent"], f["name"], f["instruction"], seconds,
                    iso_utc(next_interval(seconds)),
                )
                self._set_tab(1)
            else:
                due = initial_due(f["schedule"], f["value"], config.DEFAULT_TIMEZONE)
                self.store.create_job(
                    f["agent"], f["name"], f["instruction"], f["schedule"],
                    f["value"], config.DEFAULT_TIMEZONE, iso_utc(due),
                )
                self._set_tab(0)
            self.flash = "Created."
        except Exception as exc:
            self.flash = str(exc)

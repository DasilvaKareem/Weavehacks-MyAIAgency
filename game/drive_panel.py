"""In-game viewer for the company drive — browse every file the company owns.

The shared drive lives in SQLite (backend/store.py `files` table); agents write
to it with their drive_* tools and generated artwork/clips are auto-registered
there. This panel is the CEO's window into it: a scrollable file list on the
left, a detail pane on the right that renders text inline, previews images, and
offers to open videos/other assets in the OS player.

Like the other overlays it only ever READS on the main thread (a quick SQLite
query per refresh) and never blocks the render loop. Open with V from the office.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pyray as pr

from .chat_panel import _HAS_OPEN, _open_externally, _wrap

PANEL_W = 900
PANEL_H = 580
FONT = 18
LINE_H = 22
PAD = 18
ROW_H = 30
LIST_W = 320                      # left-hand file-list column width

BG = pr.Color(18, 22, 32, 240)
BAR = pr.Color(54, 140, 130, 255)         # teal — distinct from chat/meeting bars
FIELD = pr.Color(30, 36, 50, 255)
ROW = pr.Color(28, 32, 44, 255)
ROW_SEL = pr.Color(44, 70, 66, 255)
HEAD = pr.Color(245, 214, 120, 255)
NAME_COLOR = pr.Color(220, 230, 240, 255)
META = pr.Color(150, 160, 180, 255)
ACCENT = pr.Color(120, 210, 195, 255)
DIM = pr.Color(0, 0, 0, 140)

_IMAGE_KINDS = {"image"}
_PREVIEW_MAX = LIST_W              # cap the preview's longest edge (px)


def _btn(rect, label, base, enabled=True) -> bool:
    mouse = pr.get_mouse_position()
    hover = enabled and pr.check_collision_point_rec(mouse, rect)
    col = base if enabled else pr.Color(80, 80, 90, 255)
    if hover:
        col = pr.Color(min(col.r + 30, 255), min(col.g + 30, 255),
                       min(col.b + 30, 255), 255)
    pr.draw_rectangle_rec(rect, col)
    pr.draw_rectangle_lines_ex(rect, 2, pr.Color(10, 12, 20, 255))
    tw = pr.measure_text(label, 18)
    pr.draw_text(label, int(rect.x + (rect.width - tw) / 2),
                 int(rect.y + (rect.height - 18) / 2), 18, pr.RAYWHITE)
    return hover and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)


class DrivePanel:
    """Read-only browser over the company drive, backed by the SQL store."""

    def __init__(self, store) -> None:
        self.store = store
        self.open = False
        self._files: list = []        # FileRow list, refreshed on open
        self._sel: str | None = None  # selected file path
        self._doc_lines: list[str] = []  # wrapped text body of the selection
        self._list_scroll = 0
        self._doc_scroll = 0
        self._textures: dict[str, object] = {}  # disk_path -> Texture2D | None

    # --- lifecycle ---------------------------------------------------------

    def open_panel(self) -> None:
        self.open = True
        self._sel = None
        self._doc_lines = []
        self._list_scroll = 0
        self._doc_scroll = 0
        self._refresh()
        while pr.get_char_pressed() > 0:   # drop the 'v' that opened this
            pass

    def close(self) -> None:
        self._unload_textures()
        self.open = False
        self._sel = None
        self._files = []

    def _refresh(self) -> None:
        try:
            self._files = self.store.fs_list()      # whole drive, path-ordered
        except Exception:
            self._files = []
        # If the selection vanished (deleted elsewhere), drop the detail pane.
        if self._sel and not any(f.path == self._sel for f in self._files):
            self._sel = None
            self._doc_lines = []

    def _unload_textures(self) -> None:
        for tex in self._textures.values():
            if tex is not None:
                pr.unload_texture(tex)
        self._textures.clear()

    def _selected_file(self):
        return next((f for f in self._files if f.path == self._sel), None)

    def _select(self, f) -> None:
        self._sel = f.path
        self._doc_scroll = 0
        body_w = PANEL_W - LIST_W - 3 * PAD
        if f.content is not None:
            self._doc_lines = _wrap(f.content, body_w, FONT)
        else:
            self._doc_lines = []

    # --- generated-image preview (main-thread GL upload, same as chat) -----

    def _texture_for(self, disk_path: str):
        if disk_path in self._textures:
            return self._textures[disk_path]
        tex = None
        if disk_path and os.path.isfile(disk_path):
            img = pr.load_image(disk_path)
            if img.width > 0 and img.height > 0:
                scale = min(1.0, _PREVIEW_MAX / max(img.width, img.height))
                if scale < 1.0:
                    pr.image_resize(img, int(img.width * scale), int(img.height * scale))
                tex = pr.load_texture_from_image(img)
            pr.unload_image(img)
        self._textures[disk_path] = tex
        return tex

    # --- per-frame ---------------------------------------------------------

    def update(self) -> None:
        if not self.open:
            return
        if pr.is_key_pressed(pr.KEY_ESCAPE):
            self.close()
            return
        # Scroll wheel drives the file list or the doc body, depending on which
        # side the cursor is over.
        wheel = pr.get_mouse_wheel_move()
        if wheel:
            mx = pr.get_mouse_position().x
            sw = pr.get_screen_width()
            x = (sw - PANEL_W) // 2
            if mx < x + LIST_W + PAD:
                self._list_scroll = max(0, self._list_scroll - int(wheel))
            else:
                self._doc_scroll = max(0, self._doc_scroll - int(wheel * 3))

    # --- draw --------------------------------------------------------------

    def draw(self) -> None:
        if not self.open:
            return
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pr.draw_rectangle(0, 0, sw, sh, DIM)
        x, y = (sw - PANEL_W) // 2, (sh - PANEL_H) // 2
        pr.draw_rectangle(x, y, PANEL_W, PANEL_H, BG)
        pr.draw_rectangle(x, y, PANEL_W, 44, BAR)
        pr.draw_text("Company Drive", x + PAD, y + 11, 22, pr.RAYWHITE)
        count = f"{len(self._files)} file(s)"
        pr.draw_text(count, x + LIST_W - pr.measure_text(count, 15), y + 16, 15,
                     pr.Color(230, 245, 240, 255))

        self._draw_list(x, y)
        self._draw_detail(x, y)

        # Footer buttons (right-aligned).
        foot = y + PANEL_H - 50
        if _btn(pr.Rectangle(x + PANEL_W - PAD - 120, foot, 120, 36),
                "Close", pr.Color(70, 90, 130, 255)):
            self.close()
        if _btn(pr.Rectangle(x + PANEL_W - PAD - 250, foot, 120, 36),
                "Refresh", pr.Color(60, 110, 100, 255)):
            self._refresh()

    def _draw_list(self, x: int, y: int) -> None:
        top = y + 56
        list_h = PANEL_H - (top - y) - 16
        rows_fit = max(1, list_h // ROW_H)
        n = len(self._files)
        self._list_scroll = min(self._list_scroll, max(0, n - rows_fit))
        view = self._files[self._list_scroll:self._list_scroll + rows_fit]
        mouse = pr.get_mouse_position()
        if not self._files:
            pr.draw_text("The drive is empty.", x + PAD, top + 6, FONT, META)
        for i, f in enumerate(view):
            ry = top + i * ROW_H
            row = pr.Rectangle(x + PAD, ry, LIST_W - PAD, ROW_H - 4)
            sel = f.path == self._sel
            pr.draw_rectangle_rec(row, ROW_SEL if sel else ROW)
            if sel:
                pr.draw_rectangle(int(row.x), int(row.y), 3, int(row.height), ACCENT)
            icon = {"image": "[img]", "video": "[vid]", "audio": "[aud]",
                    "pdf": "[pdf]", "document": "[doc]", "binary": "[bin]",
                    "webapp": "[app]", "link": "[app]"}.get(f.kind, "[txt]")
            label = f"{icon} {f.path}"
            while pr.measure_text(label, 15) > row.width - 16 and len(label) > 8:
                label = label[:-2]
            pr.draw_text(label, int(row.x) + 8, int(ry) + 6, 15, NAME_COLOR)
            if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT) \
                    and pr.check_collision_point_rec(mouse, row):
                self._select(f)
        if self._list_scroll > 0:
            pr.draw_text("^", x + LIST_W - 18, top - 2, 15, META)
        if self._list_scroll + rows_fit < n:
            pr.draw_text("v", x + LIST_W - 18, top + list_h - 16, 15, META)
        # Divider between list and detail.
        pr.draw_rectangle(x + LIST_W + PAD // 2, top, 1, list_h,
                          pr.Color(60, 70, 88, 255))

    def _draw_detail(self, x: int, y: int) -> None:
        dx = x + LIST_W + PAD
        top = y + 56
        f = self._selected_file()
        if f is None:
            pr.draw_text("Select a file to view it.", dx, top + 6, FONT, META)
            return
        # Header: path + provenance.
        pr.draw_text(f.path, dx, top, FONT, HEAD)
        meta = f"{f.kind} · {f.size}{'c' if f.kind == 'text' else 'b'} · by {f.author_name} · {f.updated_at}"
        pr.draw_text(meta, dx, top + 24, 14, META)
        body_top = top + 52
        body_w = PANEL_W - LIST_W - 3 * PAD
        body_h = PANEL_H - (body_top - y) - 60

        if f.kind in ("webapp", "link"):
            # A live URL (deployed site / preview / doc). The office panel can't
            # embed a browser, so open it in the OS browser — rendered, like Lovable.
            url = (f.content or "").strip()
            pr.draw_text("Live app:", dx, body_top, FONT, NAME_COLOR)
            for i, wl in enumerate(_wrap(url, PANEL_W - LIST_W - 3 * PAD, 14)):
                pr.draw_text(wl, dx, body_top + 26 + i * 18, 14, ACCENT)
            can_open = bool(url) and _HAS_OPEN
            if _btn(pr.Rectangle(dx, body_top + 72, 200, 38),
                    "Open app in browser" if can_open else "No URL",
                    pr.Color(70, 150, 120, 255), enabled=can_open) and can_open:
                _open_externally(url)
            return

        if f.kind in _IMAGE_KINDS:
            tex = self._texture_for(f.disk_path or "")
            if tex is not None:
                pr.draw_texture(tex, dx, body_top, pr.WHITE)
            else:
                pr.draw_text("[image not found on disk]", dx, body_top, FONT, META)
                if f.disk_path:
                    pr.draw_text(f.disk_path, dx, body_top + 24, 14, META)
            return

        if f.content is None:
            # Video/audio/pdf/document/binary asset — the office panel can't render
            # these, so hand it to the OS (browser drive shows many of them inline).
            pr.draw_text(f"{f.kind} file stored on disk:", dx, body_top, FONT, NAME_COLOR)
            pr.draw_text(f.disk_path or "(no path)", dx, body_top + 26, 14, META)
            pr.draw_text("Open the browser drive to view this inline.",
                         dx, body_top + 46, 13, META)
            exists = bool(f.disk_path) and os.path.isfile(f.disk_path)
            can_open = exists and _HAS_OPEN
            if _btn(pr.Rectangle(dx, body_top + 76, 200, 38),
                    "Open externally" if can_open else "Unavailable",
                    pr.Color(60, 110, 100, 255), enabled=can_open) and can_open:
                _open_externally(f.disk_path)
            return

        # HTML text gets an "Open in browser" action so the CEO sees the RENDERED
        # page (not the source) without leaving for the terminal. The source still
        # shows below it. Everything is text so the scroll body just starts lower.
        is_html = f.name.lower().endswith((".html", ".htm"))
        if is_html and _HAS_OPEN:
            if _btn(pr.Rectangle(dx, body_top, 220, 34), "Open page in browser",
                    pr.Color(70, 150, 120, 255)):
                self._open_html(f)
            body_top += 44
            body_h -= 44

        # Text document — scrollable body.
        visible = max(1, body_h // LINE_H)
        max_scroll = max(0, len(self._doc_lines) - visible)
        self._doc_scroll = min(self._doc_scroll, max_scroll)
        start = self._doc_scroll
        ty = body_top
        for line in self._doc_lines[start:start + visible]:
            pr.draw_text(line, dx, ty, FONT, NAME_COLOR)
            ty += LINE_H
        if max_scroll > 0:
            bar = f"{start + 1}-{min(start + visible, len(self._doc_lines))} / {len(self._doc_lines)} lines"
            pr.draw_text(bar, dx, y + PANEL_H - 44, 13, META)

    def _open_html(self, f) -> None:
        """Materialize the file's whole folder to a temp dir and open the page in
        the OS browser, so a saved site renders with its sibling CSS/JS/images
        (relative links) resolving. Single-folder sites; nested dirs are flattened.
        """
        try:
            tmp = Path(tempfile.mkdtemp(prefix="company_site_"))
            for sib in self.store.fs_list(folder=f.folder):
                dest = tmp / sib.name
                if sib.content is not None:
                    dest.write_text(sib.content, errors="replace")
                elif sib.disk_path and os.path.isfile(sib.disk_path):
                    shutil.copy(sib.disk_path, dest)
            _open_externally(str(tmp / f.name))
        except Exception:
            pass   # best-effort; the browser drive is always the reliable path

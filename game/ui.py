"""2D HUD overlay drawn on top of the 3D scene."""
from __future__ import annotations

import math

import pyray as pr

from . import config, gamepad, roster

KEYBOARD_HINT = ("WASD move  -  Shift sprint  -  Space jump  -  Right-drag/Q/E look  -  "
                 "wheel zoom  -  Tab select  -  F talk")
GAMEPAD_HINT = ("L-stick move  -  R2 sprint  -  Cross jump  -  R-stick look (R3 recenter)  -  "
                "L1/R1 zoom  -  D-pad select  -  Triangle talk  -  Square hire")


class Button:
    def __init__(self, x: int, y: int, w: int, h: int, label: str) -> None:
        self.rect = pr.Rectangle(x, y, w, h)
        self.label = label
        self.enabled = True

    def draw(self) -> bool:
        """Draw the button; return True if clicked this frame."""
        mouse = pr.get_mouse_position()
        hover = pr.check_collision_point_rec(mouse, self.rect)
        if not self.enabled:
            base = pr.Color(120, 120, 130, 255)
        elif hover:
            base = pr.Color(70, 130, 220, 255)
        else:
            base = pr.Color(45, 100, 190, 255)
        pr.draw_rectangle_rec(self.rect, base)
        pr.draw_rectangle_lines_ex(self.rect, 2, pr.Color(20, 40, 90, 255))
        tw = pr.measure_text(self.label, 20)
        pr.draw_text(
            self.label,
            int(self.rect.x + (self.rect.width - tw) / 2),
            int(self.rect.y + (self.rect.height - 20) / 2),
            20,
            pr.RAYWHITE,
        )
        return (
            self.enabled
            and hover
            and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)
        )


def draw_hud(company_name: str, cash: int, agent_count: int, hire_cost: int,
             selected=None) -> None:
    # Top bar
    pr.draw_rectangle(0, 0, pr.get_screen_width(), 56, pr.Color(20, 24, 34, 230))
    pr.draw_text(company_name, 18, 14, 28, pr.RAYWHITE)
    pr.draw_text(f"Cash: ${cash:,}", 360, 18, 22, pr.GOLD)
    pr.draw_text(f"Agents: {agent_count}", 560, 18, 22, pr.SKYBLUE)
    if selected is not None:
        pr.draw_text(f"Selected: {selected.name} - {selected.role}",
                     760, 18, 22, pr.Color(70, 200, 120, 255))

    # Hint — swap to the controller layout when a pad is connected.
    hint = GAMEPAD_HINT if gamepad.available() else KEYBOARD_HINT
    pr.draw_text(hint, 18, pr.get_screen_height() - 28, 18, pr.LIGHTGRAY)


# Name-tag culling so a crowded office doesn't become an unreadable wall of text:
# only tag characters in front of and near the camera, draw the nearest first, and
# skip any tag whose box would overlap one already placed this frame.
_LABEL_MAX_DIST = 14.0     # world units; past this an agent gets no floating tag
_LABEL_FADE_DIST = 9.0     # tags start fading out beyond this
_LABEL_MAX = 7             # hard cap on simultaneous tags (nearest win)


def _rects_overlap(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def draw_world_labels(characters, camera) -> None:
    cam, tgt = camera.position, camera.target
    fx, fy, fz = tgt.x - cam.x, tgt.y - cam.y, tgt.z - cam.z   # camera forward

    # Gather visible candidates with their distance, nearest-first.
    cands = []
    for ch in characters:
        a = ch.label_anchor
        dx, dy, dz = a.x - cam.x, a.y - cam.y, a.z - cam.z
        if dx * fx + dy * fy + dz * fz <= 0:        # behind the camera
            continue
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dist > _LABEL_MAX_DIST:
            continue
        cands.append((dist, ch))
    cands.sort(key=lambda t: t[0])

    sw_, sh_ = pr.get_screen_width(), pr.get_screen_height()
    placed: list[tuple[int, int, int, int]] = []
    for dist, ch in cands:
        if len(placed) >= _LABEL_MAX:
            break
        sp = pr.get_world_to_screen(ch.label_anchor, camera)
        if sp.x < -60 or sp.x > sw_ + 60 or sp.y < -30 or sp.y > sh_ + 30:
            continue
        name = ch.name
        sub = ch.role if not ch.dept else f"{ch.role} · {ch.dept}"
        nw = pr.measure_text(name, 16)
        sw = pr.measure_text(sub, 13)
        w = max(nw, sw)
        rx, ry = int(sp.x - w / 2) - 6, int(sp.y) - 3
        box = (rx, ry, w + 12, 38)
        if any(_rects_overlap(box, p) for p in placed):   # don't stack on a neighbour
            continue
        # Fade distant tags so nearby ones stand out.
        t = 1.0 if dist <= _LABEL_FADE_DIST else \
            max(0.0, 1.0 - (dist - _LABEL_FADE_DIST) / (_LABEL_MAX_DIST - _LABEL_FADE_DIST))
        a = int(40 + 215 * t)
        pr.draw_rectangle(rx, ry, w + 12, 38, pr.Color(0, 0, 0, int(150 * t)))
        pr.draw_text(name, int(sp.x - nw / 2), int(sp.y), 16, pr.Color(255, 255, 255, a))
        pr.draw_text(sub, int(sp.x - sw / 2), int(sp.y) + 18, 13,
                     pr.Color(170, 200, 230, a))
        placed.append(box)


def _panel_button(rect, label, base, mouse) -> bool:
    hover = pr.check_collision_point_rec(mouse, rect)
    col = pr.Color(min(base.r + 30, 255), min(base.g + 30, 255), min(base.b + 30, 255), 255) if hover else base
    pr.draw_rectangle_rec(rect, col)
    pr.draw_rectangle_lines_ex(rect, 2, pr.Color(10, 12, 20, 255))
    tw = pr.measure_text(label, 18)
    pr.draw_text(label, int(rect.x + (rect.width - tw) / 2), int(rect.y + (rect.height - 18) / 2), 18, pr.RAYWHITE)
    return hover and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)


def swatch_row(x: int, y: int, palette: list, sel: int, mouse, size: int = 40,
               gap: int = 10) -> int:
    """Draw a color palette as clickable swatches; return the (possibly new) index.

    Shared by the CEO builder and the hire dialog so both customizers look alike."""
    for i, (_, (r, g, b)) in enumerate(palette):
        rect = pr.Rectangle(x + i * (size + gap), y, size, size)
        pr.draw_rectangle_rec(rect, pr.Color(r, g, b, 255))
        if i == sel:
            pr.draw_rectangle_lines_ex(rect, 4, pr.RAYWHITE)
        else:
            pr.draw_rectangle_lines_ex(rect, 1, pr.Color(0, 0, 0, 180))
        if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT) \
                and pr.check_collision_point_rec(mouse, rect):
            sel = i
    return sel


def stepper(x: int, y: int, w: int, options: list, sel: int, mouse) -> int:
    """A ◀ label ▶ cycler for a list of (label, ...) options; returns the index."""
    h = 38
    for bx, step in ((x, -1), (x + w - h, +1)):
        rect = pr.Rectangle(bx, y, h, h)
        hot = pr.check_collision_point_rec(mouse, rect)
        pr.draw_rectangle_rec(rect, pr.Color(58, 66, 88, 255) if hot else pr.Color(46, 52, 70, 255))
        pr.draw_rectangle_lines_ex(rect, 1, pr.Color(0, 0, 0, 120))
        pr.draw_text("<" if step < 0 else ">", int(bx) + 14, int(y) + 9, 20, pr.RAYWHITE)
        if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT) and hot:
            sel = (sel + step) % len(options)
    box = pr.Rectangle(x + h + 6, y, w - 2 * (h + 6), h)
    pr.draw_rectangle_rec(box, pr.Color(16, 19, 27, 255))
    pr.draw_rectangle_lines_ex(box, 1, pr.Color(70, 130, 220, 255))
    name = options[sel][0]
    tw = pr.measure_text(name, 20)
    pr.draw_text(name, int(box.x + (box.width - tw) / 2), int(y) + 9, 20, pr.GOLD)
    return sel


class HireDialog:
    """Modal shown at hire time: a full character customizer (role, skin, hair,
    hairstyle, eyes) matching the CEO builder, then confirm or cancel."""

    def __init__(self) -> None:
        self.open = False
        self.candidate: dict | None = None
        self.tone_idx = 0          # skin
        self.role_idx = 0
        self.hair_idx = 0
        self.hair_style = 0
        self.eye_idx = 0

    def open_for(self, candidate: dict) -> None:
        self.candidate = candidate
        self.tone_idx = candidate.get("tone_idx", 0)
        self.hair_idx = candidate.get("hair_idx", 0)
        self.hair_style = candidate.get("hair_style", 0)
        self.eye_idx = candidate.get("eye_idx", 0)
        self.role_idx = next(
            (i for i, (t, _, _) in enumerate(roster.ROLES) if t == candidate["role"]),
            0,
        )
        self.open = True

    def close(self) -> None:
        self.open = False
        self.candidate = None

    def appearance(self) -> dict:
        """The chosen look as stable indices (persisted as the hire's char_appearance)."""
        return {
            "skin_idx": self.tone_idx,
            "hair_idx": self.hair_idx,
            "hair_style": self.hair_style,
            "eye_idx": self.eye_idx,
            "suit_idx": 0,            # suit tint only matters on suit models; default
        }

    def _apply_role(self, idx: int) -> None:
        """Switch the candidate to roster.ROLES[idx] (role drives dept + color)."""
        self.role_idx = idx % len(roster.ROLES)
        title, dept, color = roster.ROLES[self.role_idx]
        self.candidate["role"] = title
        self.candidate["dept"] = dept
        self.candidate["color"] = color

    def draw(self) -> str | None:
        """Render the modal; return 'hire', 'cancel', or None."""
        if not self.open or self.candidate is None:
            return None
        c = self.candidate
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        w, h = 560, 500
        x, y = (sw - w) // 2, (sh - h) // 2
        mouse = pr.get_mouse_position()
        label_col = pr.Color(150, 160, 180, 255)

        pr.draw_rectangle(0, 0, sw, sh, pr.Color(0, 0, 0, 130))         # dim backdrop
        pr.draw_rectangle(x, y, w, h, pr.Color(28, 32, 44, 255))
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 2, pr.Color(70, 130, 220, 255))

        pr.draw_text("New Hire", x + 22, y + 16, 26, pr.RAYWHITE)

        # Editable name field + Shuffle.  Type to rename; click Shuffle to reroll.
        name_box = pr.Rectangle(x + 22, y + 50, w - 156, 32)
        pr.draw_rectangle_rec(name_box, pr.Color(20, 24, 34, 255))
        pr.draw_rectangle_lines_ex(name_box, 1, pr.Color(70, 130, 220, 255))
        chc = pr.get_char_pressed()
        while chc > 0:
            if 32 <= chc < 127 and len(c["name"]) < 24:
                c["name"] += chr(chc)
            chc = pr.get_char_pressed()
        bs = pr.is_key_pressed(pr.KEY_BACKSPACE)
        if hasattr(pr, "is_key_pressed_repeat"):
            bs = bs or pr.is_key_pressed_repeat(pr.KEY_BACKSPACE)
        if bs and c["name"]:
            c["name"] = c["name"][:-1]
        caret = "_" if (pr.get_time() % 1.0) < 0.5 else ""
        pr.draw_text(c["name"] + caret, int(name_box.x) + 8, int(name_box.y) + 7, 20, pr.GOLD)
        if _panel_button(pr.Rectangle(x + w - 124, y + 50, 102, 32), "Shuffle",
                         pr.Color(60, 70, 95, 255), mouse):
            c["name"] = roster.random_name()

        # Role picker:  [<]  Role · Dept  [>]   (also Up/Down keys)
        role_y = y + 94
        left = pr.Rectangle(x + 22, role_y, 26, 26)
        right = pr.Rectangle(x + w - 48, role_y, 26, 26)
        if _panel_button(left, "<", pr.Color(50, 60, 80, 255), mouse) or pr.is_key_pressed(pr.KEY_UP):
            self._apply_role(self.role_idx - 1)
        if _panel_button(right, ">", pr.Color(50, 60, 80, 255), mouse) or pr.is_key_pressed(pr.KEY_DOWN):
            self._apply_role(self.role_idx + 1)
        role_txt = f'{c["role"]} · {c["dept"]}'
        rtw = pr.measure_text(role_txt, 18)
        pr.draw_text(role_txt, x + 56 + ((w - 112) - rtw) // 2, role_y + 4, 18, pr.SKYBLUE)

        # Appearance rows — skin / hair / hairstyle / eyes (mirrors the CEO builder).
        pr.draw_text("SKIN TONE", x + 22, y + 132, 15, label_col)
        self.tone_idx = swatch_row(x + 22, y + 152, config.SKIN_TONES, self.tone_idx, mouse)

        pr.draw_text("HAIR", x + 22, y + 202, 15, label_col)
        self.hair_idx = swatch_row(x + 22, y + 222, config.HAIR_COLORS, self.hair_idx, mouse)

        pr.draw_text("HAIRSTYLE", x + 22, y + 272, 15, label_col)
        self.hair_style = stepper(x + 22, y + 292, 360, config.HAIRSTYLES, self.hair_style, mouse)

        pr.draw_text("EYES", x + 22, y + 342, 15, label_col)
        self.eye_idx = swatch_row(x + 22, y + 362, config.EYE_COLORS, self.eye_idx, mouse)

        pr.draw_text("Type to name   ·   < > / Up,Down: role   ·   click swatches to style",
                     x + 22, y + h - 70, 13, label_col)

        cancel = pr.Rectangle(x + 22, y + h - 52, 120, 38)
        hire = pr.Rectangle(x + w - 162, y + h - 52, 140, 38)
        do_cancel = _panel_button(cancel, "Cancel", pr.Color(120, 60, 60, 255), mouse)
        do_hire = _panel_button(hire, "Confirm", pr.Color(45, 140, 80, 255), mouse)

        confirm = do_hire or pr.is_key_pressed(pr.KEY_ENTER) or gamepad.pressed(gamepad.CROSS)
        if confirm and c["name"].strip():
            return "hire"
        if do_cancel or pr.is_key_pressed(pr.KEY_ESCAPE) or gamepad.pressed(gamepad.TRIANGLE):
            return "cancel"
        return None

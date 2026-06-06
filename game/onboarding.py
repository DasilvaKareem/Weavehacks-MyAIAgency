"""First-launch CEO-creation tutorial.

Shown once, the very first time the game runs (when no CEO profile is saved
yet). The player names their CEO and customizes gender (male/female suit),
skin tone, hair color and suit color, watching a live rotating 3D preview.
On confirm, `draw()` returns the chosen profile dict; the game persists it and
drops the player into the office park. Returning players never see this — the
saved profile loads straight in.

Self-contained like the park frame: it runs its own begin/end_drawing.
"""
from __future__ import annotations

import pyray as pr

from . import config, roster
from .entities import Character
from .ui import _panel_button

_BG = pr.Color(18, 21, 30, 255)
_PANEL = pr.Color(26, 30, 42, 255)
_ACCENT = pr.Color(70, 130, 220, 255)


class OnboardingScreen:
    """Modal CEO builder. Call draw(registry) each frame; it returns the profile
    dict once the player confirms, else None."""

    def __init__(self) -> None:
        self.name = ""
        self.gender = "male"          # "male" | "female"
        self.skin_idx = 1             # default "Fair"
        self.hair_idx = 0
        self.hair_style = 0           # index into config.HAIRSTYLES
        self.suit_idx = 0
        self.eye_idx = 0              # eye color (the "Face" material)
        self._pending_unlock = None   # suit idx awaiting an unlock-confirm popup
        self.first_run = True         # first launch (vs re-editing via Settings)
        self._spin = 0.0              # preview turntable angle (degrees)
        # Live preview character (positioned at the origin, spun in place).
        self._preview = Character(name="", role="CEO", x=0.0, z=0.0,
                                  color=pr.GOLD, model=self._model())
        self._cam = pr.Camera3D(
            pr.Vector3(0.0, 1.35, 3.5), pr.Vector3(0.0, 0.95, 0.0),
            pr.Vector3(0.0, 1.0, 0.0), 45.0, pr.CAMERA_PERSPECTIVE,
        )
        # The 3D preview renders into its own texture (sized to the left panel) so
        # the model frames inside that panel instead of the full window. Created
        # lazily once the GL context exists; freed by dispose().
        self._rt = None

    def dispose(self) -> None:
        """Free the preview render texture (call once onboarding is finished)."""
        if self._rt is not None:
            pr.unload_render_texture(self._rt)
            self._rt = None

    def open_with(self, profile: dict | None) -> None:
        """Pre-fill the editor from an existing profile (used by the Settings
        button to re-edit the CEO). A default/blank name shows the placeholder."""
        self.first_run = not profile          # re-edit when given an existing profile
        profile = profile or {}
        name = profile.get("name") or ""
        self.name = "" if name == "You (CEO)" else name
        self.gender = profile.get("gender", "male")
        self.skin_idx = profile.get("skin_idx", 1)
        self.hair_idx = profile.get("hair_idx", 0)
        self.hair_style = profile.get("hair_style", 0)
        self.suit_idx = profile.get("suit_idx", 0)
        self.eye_idx = profile.get("eye_idx", 0)
        self._pending_unlock = None

    # -- profile ------------------------------------------------------------
    def _model(self) -> str:
        return config.CEO_MODEL_MALE if self.gender == "male" else config.CEO_MODEL_FEMALE

    def profile(self) -> dict:
        """The chosen CEO profile (stored as stable indices + derived model)."""
        return {
            "name": self.name.strip() or "You (CEO)",
            "gender": self.gender,
            "model": self._model(),
            "skin_idx": self.skin_idx,
            "hair_idx": self.hair_idx,
            "hair_style": self.hair_style,
            "suit_idx": self.suit_idx,
            "eye_idx": self.eye_idx,
        }

    # -- input helpers ------------------------------------------------------
    def _edit_name(self) -> None:
        chc = pr.get_char_pressed()
        while chc > 0:
            if 32 <= chc < 127 and len(self.name) < 22:
                self.name += chr(chc)
            chc = pr.get_char_pressed()
        bs = pr.is_key_pressed(pr.KEY_BACKSPACE)
        if hasattr(pr, "is_key_pressed_repeat"):
            bs = bs or pr.is_key_pressed_repeat(pr.KEY_BACKSPACE)
        if bs and self.name:
            self.name = self.name[:-1]

    def _swatch_row(self, x: int, y: int, palette: list, sel: int, mouse) -> int:
        """Draw a palette as clickable swatches; return the (possibly new) index."""
        size, gap = 46, 12
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

    def _suit_row(self, x: int, y: int, sel: int, mouse, unlocked: set) -> int:
        """Like _swatch_row, but premium suits show a padlock until purchased.
        Clicking a locked swatch opens the unlock popup instead of selecting it."""
        size, gap = 46, 12
        for i, (_, (r, g, b)) in enumerate(config.SUIT_COLORS):
            rect = pr.Rectangle(x + i * (size + gap), y, size, size)
            locked = i in config.SUIT_UNLOCKS and config.suit_outfit_id(i) not in unlocked
            pr.draw_rectangle_rec(rect, pr.Color(r, g, b, 255))
            if locked:                                  # dim + padlock overlay
                pr.draw_rectangle_rec(rect, pr.Color(0, 0, 0, 110))
                lx, ly = int(rect.x + size / 2 - 6), int(rect.y + size / 2 - 7)
                pr.draw_rectangle(lx, ly + 6, 12, 9, pr.Color(230, 200, 90, 255))
                pr.draw_ring(pr.Vector2(lx + 6, ly + 6), 3, 5, 180, 360, 16,
                             pr.Color(230, 200, 90, 255))
            pr.draw_rectangle_lines_ex(rect, 4 if i == sel else 1,
                                       pr.RAYWHITE if i == sel else pr.Color(0, 0, 0, 180))
            if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT) \
                    and pr.check_collision_point_rec(mouse, rect):
                if locked:
                    self._pending_unlock = i            # ask before charging
                else:
                    sel = i
        return sel

    def _unlock_popup(self, cash: int, unlocked: set, on_unlock) -> None:
        """Modal confirm for buying the pending premium suit. Buys via on_unlock
        (which charges + persists + grows `unlocked`); on success, selects the suit."""
        idx = self._pending_unlock
        price = config.SUIT_UNLOCKS[idx]
        name = config.SUIT_COLORS[idx][0]
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        w, h = 420, 180
        x, y = (sw - w) // 2, (sh - h) // 2
        mouse = pr.get_mouse_position()
        pr.draw_rectangle(0, 0, sw, sh, pr.Color(0, 0, 0, 150))
        pr.draw_rectangle(x, y, w, h, pr.Color(30, 34, 46, 255))
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 2, _ACCENT)
        pr.draw_text(f"Unlock {name} suit", x + 22, y + 20, 24, pr.RAYWHITE)
        afford = cash >= price
        pr.draw_text(f"One-time:  ${price:,}", x + 22, y + 58, 20,
                     pr.GOLD if afford else pr.Color(200, 90, 90, 255))
        pr.draw_text(f"Cash:  ${cash:,}", x + 22, y + 86, 16, pr.Color(150, 165, 190, 255))
        buy_rect = pr.Rectangle(x + 22, y + h - 52, 180, 38)
        cancel_rect = pr.Rectangle(x + w - 142, y + h - 52, 120, 38)
        buy_col = pr.Color(45, 140, 80, 255) if afford else pr.Color(70, 74, 84, 255)
        buy = _panel_button(buy_rect, "Buy" if afford else "Too dear", buy_col, mouse)
        cancel = (_panel_button(cancel_rect, "Cancel", pr.Color(120, 60, 60, 255), mouse)
                  or pr.is_key_pressed(pr.KEY_ESCAPE))
        if buy and afford and on_unlock is not None and on_unlock(config.suit_outfit_id(idx), price):
            self.suit_idx = idx
            self._pending_unlock = None
        elif cancel:
            self._pending_unlock = None

    def _stepper(self, x: int, y: int, w: int, options: list, sel: int, mouse) -> int:
        """A ◀ label ▶ cycler for a list of (label, ...) options; returns the index."""
        h = 38
        for bx, step in ((x, -1), (x + w - h, +1)):
            rect = pr.Rectangle(bx, y, h, h)
            hot = pr.check_collision_point_rec(mouse, rect)
            pr.draw_rectangle_rec(rect, pr.Color(58, 66, 88, 255) if hot else pr.Color(46, 52, 70, 255))
            pr.draw_rectangle_lines_ex(rect, 1, pr.Color(0, 0, 0, 120))
            arrow = "<" if step < 0 else ">"
            pr.draw_text(arrow, int(bx) + 14, int(y) + 9, 20, pr.RAYWHITE)
            if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT) and hot:
                sel = (sel + step) % len(options)
        box = pr.Rectangle(x + h + 6, y, w - 2 * (h + 6), h)
        pr.draw_rectangle_rec(box, pr.Color(16, 19, 27, 255))
        pr.draw_rectangle_lines_ex(box, 1, _ACCENT)
        name = options[sel][0]
        tw = pr.measure_text(name, 20)
        pr.draw_text(name, int(box.x + (box.width - tw) / 2), int(y) + 9, 20, pr.GOLD)
        return sel

    # -- frame --------------------------------------------------------------
    def draw(self, registry, unlocked=None, cash: int = 0, on_unlock=None) -> dict | None:
        dt = pr.get_frame_time()
        self._spin = (self._spin + dt * 35.0) % 360.0
        mouse = pr.get_mouse_position()
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        unlocked = unlocked if unlocked is not None else set()

        # While the unlock popup is up it captures typing/clicks; freeze name edits.
        if self._pending_unlock is None:
            self._edit_name()

        # Sync the preview to the current selections.
        p = self._preview
        p.model = self._model()
        p.yaw = self._spin
        p.hair_style = self.hair_style
        p.skin_tone = roster.tone_color(self.skin_idx)
        p.hair_tone = roster.palette_color(config.HAIR_COLORS, self.hair_idx)
        p.outfit_tone = roster.palette_color(config.SUIT_COLORS, self.suit_idx)
        p.eye_tone = roster.palette_color(config.EYE_COLORS, self.eye_idx)
        p.update(dt, registry)

        # --- left: live 3D preview, rendered into its own panel-sized texture
        pw = 540
        if self._rt is None:
            self._rt = pr.load_render_texture(pw, sh)
        pr.begin_texture_mode(self._rt)
        pr.clear_background(_PANEL)
        pr.begin_mode_3d(self._cam)
        pr.draw_cylinder(pr.Vector3(0.0, 0.0, 0.0), 1.05, 1.05, 0.04, 32,
                         pr.Color(40, 46, 62, 255))
        pr.draw_cylinder_wires(pr.Vector3(0.0, 0.0, 0.0), 1.05, 1.05, 0.04, 32,
                               pr.Color(70, 80, 105, 255))
        p.draw(registry)
        pr.end_mode_3d()
        pr.end_texture_mode()

        pr.begin_drawing()
        pr.clear_background(_BG)
        # Blit the preview (render textures are y-flipped → negative source height).
        pr.draw_texture_rec(self._rt.texture, pr.Rectangle(0, 0, pw, -sh),
                            pr.Vector2(0, 0), pr.WHITE)
        sub = "Step 1 of 1 — meet your founder"
        pr.draw_text(sub, 28, sh - 44, 18, pr.Color(150, 165, 190, 255))

        # --- right: controls -----------------------------------------------
        cx = pw + 44
        label_col = pr.Color(150, 165, 190, 255)
        pr.draw_text("Create your CEO", cx, 36, 34, pr.RAYWHITE)
        pr.draw_text("This is you. Make them yours, then step into the park.",
                     cx, 76, 16, label_col)

        # Name
        pr.draw_text("NAME", cx, 116, 15, label_col)
        name_box = pr.Rectangle(cx, 136, 500, 38)
        pr.draw_rectangle_rec(name_box, pr.Color(16, 19, 27, 255))
        pr.draw_rectangle_lines_ex(name_box, 1, _ACCENT)
        caret = "_" if (pr.get_time() % 1.0) < 0.5 else ""
        if self.name:
            pr.draw_text(self.name + caret, int(name_box.x) + 10, int(name_box.y) + 9, 20, pr.GOLD)
        else:
            pr.draw_text("Type a name" + caret, int(name_box.x) + 10, int(name_box.y) + 9,
                         20, pr.Color(110, 120, 140, 255))

        # Gender
        pr.draw_text("GENDER", cx, 190, 15, label_col)
        male_btn = pr.Rectangle(cx, 210, 120, 36)
        female_btn = pr.Rectangle(cx + 132, 210, 120, 36)
        male_col = pr.Color(45, 140, 80, 255) if self.gender == "male" else pr.Color(50, 56, 74, 255)
        female_col = pr.Color(45, 140, 80, 255) if self.gender == "female" else pr.Color(50, 56, 74, 255)
        if _panel_button(male_btn, "Male", male_col, mouse):
            self.gender = "male"
        if _panel_button(female_btn, "Female", female_col, mouse):
            self.gender = "female"

        # Rows: skin → hair colour → hairstyle → eyes → suit (each its own control).
        pr.draw_text("SKIN TONE", cx, 250, 15, label_col)
        self.skin_idx = self._swatch_row(cx, 268, config.SKIN_TONES, self.skin_idx, mouse)

        pr.draw_text("HAIR", cx, 326, 15, label_col)
        self.hair_idx = self._swatch_row(cx, 344, config.HAIR_COLORS, self.hair_idx, mouse)

        pr.draw_text("HAIRSTYLE", cx, 402, 15, label_col)
        self.hair_style = self._stepper(cx, 420, 360, config.HAIRSTYLES, self.hair_style, mouse)

        pr.draw_text("EYES", cx, 470, 15, label_col)
        self.eye_idx = self._swatch_row(cx, 488, config.EYE_COLORS, self.eye_idx, mouse)

        pr.draw_text("SUIT", cx, 546, 15, label_col)
        self.suit_idx = self._suit_row(cx, 564, self.suit_idx, mouse, unlocked)

        # Confirm (+ Cancel when re-editing from Settings)
        confirm_label = "Enter the Park" if self.first_run else "Save"
        start = pr.Rectangle(cx, 628, 320, 56)
        go = _panel_button(start, confirm_label, pr.Color(45, 140, 80, 255), mouse)
        cancel = False
        if not self.first_run:
            cancel_rect = pr.Rectangle(cx + 336, 628, 130, 56)
            cancel = (_panel_button(cancel_rect, "Cancel", pr.Color(120, 60, 60, 255), mouse)
                      or pr.is_key_pressed(pr.KEY_ESCAPE))
        else:
            pr.draw_text("Enter to confirm", cx + 336, 646, 16, pr.Color(130, 140, 160, 255))

        # Premium-suit unlock popup (drawn on top); it swallows confirm/cancel below.
        if self._pending_unlock is not None:
            self._unlock_popup(cash, unlocked, on_unlock)
            go = cancel = False

        pr.end_drawing()

        if self._pending_unlock is not None:
            return None
        if cancel:
            return "cancel"
        if go or pr.is_key_pressed(pr.KEY_ENTER):
            return self.profile()
        return None

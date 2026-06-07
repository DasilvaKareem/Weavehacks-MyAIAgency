"""The opening prologue — a guided, Animal-Crossing-style cold open.

The very first time you play, a relocation guide (Sam) meets you right after your
old city rejected your pitch. Through the conversation you set up your founder,
name the company, and give your one-line pitch — then the screen fades on a
"A NEW CITY" card and drops you into the park to begin the real game.

It's a small step machine over a script of beats:
  - say   : a line of dialogue from the guide (typewriter; click/Enter to advance)
  - create: hand the whole frame to the existing character creator
  - ask   : a typed answer (company name, pitch) stored on the profile
  - fade  : fade to black + title card, then finish
`draw()` returns the assembled profile dict once the last beat is done; main.py
saves it and enters the park. Self-contained frame, like OnboardingScreen.
"""
from __future__ import annotations

import pyray as pr

from . import config, roster
from .entities import Character
from .onboarding import OnboardingScreen

GUIDE_NAME = "Sam"
GUIDE_MODEL = "Casual_Male.gltf"     # a friendly "mover" who relocates you

_BG = pr.Color(16, 18, 26, 255)
_BOX = pr.Color(24, 28, 40, 245)
_ACCENT = pr.Color(70, 130, 220, 255)
_NAMECOL = pr.Color(120, 200, 255, 255)
_HINT = pr.Color(120, 130, 150, 255)

# The script. {company}/{name}/{pitch} are filled from answers as they come in.
_SCRIPT = [
    ("say", "Easy there. You look like a man who just got shown the door."),
    ("say", "Let me guess — you pitched 'em, and they laughed you out of the room."),
    ("say", "Idea guy. No team, no money, no office, no nothin'. Am I close?"),
    ("say", "Don't sweat it. That city wouldn't know a good idea if it signed the lease."),
    ("say", "Name's {guide}. I move people who are starting over. Folks who aren't done yet."),
    ("say", "And something tells me you're not done. So — let's get a look at you."),
    ("create",),
    ("say", "There's the founder. Now we're talking."),
    ("say", "New city, clean slate. You've got nothing but the idea in your head."),
    ("say", "So that's where you start. Name the thing, pitch it, build it — one step at a time."),
    ("say", "It's all on your list. Get to work, founder."),
    ("fade", "A NEW CITY", "You arrive with nothing but an idea."),
]

_TYPE_CPS = 42.0     # typewriter characters per second


class Prologue:
    def __init__(self) -> None:
        self.i = 0
        self.fields: dict = {}            # company_name, pitch, + creator profile
        self._reveal = 0.0                # chars revealed in the current "say"
        self._typed = ""                  # current "ask" buffer
        self._fade = 0.0                  # fade-to-black amount [0..1]
        self._fade_hold = 0.0             # seconds held on the title card
        self.creator = OnboardingScreen()
        self.creator.first_run = True
        self._guide = Character(name=GUIDE_NAME, role="Guide", x=0.0, z=0.0,
                                color=pr.GOLD, model=GUIDE_MODEL)
        # Tint to a real appearance, else the model's raw "Skin" material (~black)
        # shows and the intro guide renders solid black.
        roster.apply_look(self._guide, {"skin_idx": 3, "hair_idx": 4, "eye_idx": 1})
        self._cam = pr.Camera3D(pr.Vector3(0.0, 1.35, 3.4), pr.Vector3(0.0, 1.0, 0.0),
                                pr.Vector3(0.0, 1.0, 0.0), 45.0, pr.CAMERA_PERSPECTIVE)
        self._spin = 0.0
        self._rt = None

    def dispose(self) -> None:
        self.creator.dispose()
        if self._rt is not None:
            pr.unload_render_texture(self._rt)
            self._rt = None

    # -- helpers ------------------------------------------------------------
    def _fill(self, text: str) -> str:
        return text.format(guide=GUIDE_NAME,
                           company=self.fields.get("company_name", "your company"),
                           name=self.fields.get("name", "founder"),
                           pitch=self.fields.get("pitch", ""))

    def profile(self) -> dict:
        """The creator profile plus the prologue answers (company name + pitch)."""
        p = dict(self.fields)
        p.setdefault("name", "You (CEO)")
        return p

    # -- per-beat drawing ---------------------------------------------------
    def draw(self, registry) -> dict | None:
        if self.i >= len(_SCRIPT):
            return self.profile()
        beat = _SCRIPT[self.i]
        kind = beat[0]

        if kind == "create":
            prof = self.creator.draw(registry)      # owns the whole frame
            if isinstance(prof, dict):
                self.fields.update(prof)            # name, gender, model, tones, hair_style
                self.i += 1
            return None

        dt = pr.get_frame_time()
        self._spin = (self._spin + dt * 22.0) % 360.0
        self._guide.yaw = self._spin
        self._guide.update(dt, registry)

        pr.begin_drawing()
        pr.clear_background(_BG)
        self._draw_guide(registry)

        if kind == "say":
            self._draw_say(self._fill(beat[1]), dt)
        elif kind == "ask":
            self._draw_ask(beat[2], beat[3])
        elif kind == "fade":
            done = self._draw_fade(self._fill(beat[1]), self._fill(beat[2]), dt)
            pr.end_drawing()
            return self.profile() if done else None

        pr.end_drawing()
        return None

    def _draw_guide(self, registry) -> None:
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        if self._rt is None:
            self._rt = pr.load_render_texture(sw, sh)
        pr.begin_texture_mode(self._rt)
        pr.clear_background(_BG)
        pr.begin_mode_3d(self._cam)
        pr.draw_cylinder(pr.Vector3(0, 0, 0), 0.9, 0.9, 0.03, 28, pr.Color(34, 40, 56, 255))
        self._guide.draw(registry)
        pr.end_mode_3d()
        pr.end_texture_mode()
        pr.draw_texture_rec(self._rt.texture, pr.Rectangle(0, 0, sw, -sh),
                            pr.Vector2(0, 0), pr.WHITE)

    def _dialogue_box(self) -> pr.Rectangle:
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        return pr.Rectangle(60, sh - 220, sw - 120, 160)

    def _draw_namebox(self, box) -> None:
        pr.draw_rectangle(int(box.x), int(box.y) - 34, 180, 34, _ACCENT)
        pr.draw_text(GUIDE_NAME, int(box.x) + 16, int(box.y) - 27, 20, pr.RAYWHITE)

    def _draw_say(self, text: str, dt: float) -> None:
        box = self._dialogue_box()
        pr.draw_rectangle_rec(box, _BOX)
        pr.draw_rectangle_lines_ex(box, 2, _ACCENT)
        self._draw_namebox(box)
        self._reveal = min(len(text), self._reveal + dt * _TYPE_CPS)
        shown = text[:int(self._reveal)]
        self._wrap_text(shown, int(box.x) + 24, int(box.y) + 22, int(box.width) - 48, 24)
        full = self._reveal >= len(text)
        if full:
            pr.draw_text(">  click / Enter", int(box.x + box.width) - 200,
                         int(box.y + box.height) - 30, 18, _HINT)
        if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT) or pr.is_key_pressed(pr.KEY_ENTER) \
                or pr.is_key_pressed(pr.KEY_SPACE):
            if full:
                self.i += 1
                self._reveal = 0.0
            else:
                self._reveal = len(text)       # first click: finish the reveal

    def _draw_ask(self, label: str, placeholder: str) -> None:
        box = self._dialogue_box()
        pr.draw_rectangle_rec(box, _BOX)
        pr.draw_rectangle_lines_ex(box, 2, _ACCENT)
        self._draw_namebox(box)
        pr.draw_text(label.upper(), int(box.x) + 24, int(box.y) + 18, 15, _NAMECOL)
        field = pr.Rectangle(box.x + 24, box.y + 46, box.width - 48, 44)
        pr.draw_rectangle_rec(field, pr.Color(14, 16, 24, 255))
        pr.draw_rectangle_lines_ex(field, 1, _ACCENT)
        # capture typing
        ch = pr.get_char_pressed()
        while ch > 0:
            if 32 <= ch < 127 and len(self._typed) < 40:
                self._typed += chr(ch)
            ch = pr.get_char_pressed()
        bs = pr.is_key_pressed(pr.KEY_BACKSPACE)
        if hasattr(pr, "is_key_pressed_repeat"):
            bs = bs or pr.is_key_pressed_repeat(pr.KEY_BACKSPACE)
        if bs and self._typed:
            self._typed = self._typed[:-1]
        caret = "_" if (pr.get_time() % 1.0) < 0.5 else ""
        if self._typed:
            pr.draw_text(self._typed + caret, int(field.x) + 12, int(field.y) + 11, 22, pr.GOLD)
        else:
            pr.draw_text(placeholder + caret, int(field.x) + 12, int(field.y) + 11, 22,
                         pr.Color(110, 120, 140, 255))
        pr.draw_text("Enter to confirm", int(box.x + box.width) - 220,
                     int(box.y + box.height) - 30, 18, _HINT)
        if pr.is_key_pressed(pr.KEY_ENTER) and self._typed.strip():
            self.fields[_SCRIPT[self.i][1]] = self._typed.strip()
            self._typed = ""
            self.i += 1

    def _draw_fade(self, title: str, subtitle: str, dt: float) -> bool:
        self._fade = min(1.0, self._fade + dt * 1.1)
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        pr.draw_rectangle(0, 0, sw, sh, pr.Color(0, 0, 0, int(self._fade * 255)))
        if self._fade >= 1.0:
            tw = pr.measure_text(title, 56)
            pr.draw_text(title, sw // 2 - tw // 2, sh // 2 - 50, 56, pr.RAYWHITE)
            sub = subtitle.strip()
            if sub:
                sw2 = pr.measure_text(sub, 24)
                pr.draw_text(sub, sw // 2 - sw2 // 2, sh // 2 + 20, 24, _NAMECOL)
            self._fade_hold += dt
            return self._fade_hold > 2.2
        return False

    def _wrap_text(self, text: str, x: int, y: int, max_w: int, font: int) -> None:
        words = text.split(" ")
        line, ly = "", y
        for w in words:
            trial = (line + " " + w).strip()
            if pr.measure_text(trial, font) > max_w and line:
                pr.draw_text(line, x, ly, font, pr.RAYWHITE)
                line, ly = w, ly + font + 6
            else:
                line = trial
        if line:
            pr.draw_text(line, x, ly, font, pr.RAYWHITE)

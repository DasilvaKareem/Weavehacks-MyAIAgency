"""Cinematic recorder — GTA-style cutscenes, no GUI.

This is a small *timeline* engine layered on top of the existing 3D world. A
cutscene is a global clock (t = 0 → total seconds) with parallel tracks, exactly
like a real cinematic editor (or GTA's cutscene files):

    CAMERA   one active Shot at a time → cuts between angles
    ACTORS   each Character runs a list of timed beats (walk / hold / play)
    CAPTIONS lower-third lines that appear for a window of time

Every frame the Director samples the clock: it picks the active camera Shot,
poses every actor, and hands a clean `pr.Camera3D` to the caller. The caller
draws the world *without* any HUD (that's the "no GUI" part) into an off-screen
RenderTexture, and the Recorder dumps each frame to a PNG sequence. A short
helper then stitches the frames into an .mp4 with MoviePy (which bundles its own
ffmpeg, so nothing has to be installed at the OS level).

Authoring a cutscene = building a `Scene` (see cinematic_demo.py): place a few
actors, give them beats, lay out a list of camera Shots, press record.

Nothing here is wired into the live game loop; it's a standalone tool so it can
run the world deterministically (fixed dt) for buttery, machine-independent
footage. raylib still needs a GL context to render, so the *render* step needs a
display (a desktop is fine; a headless box needs xvfb). Encoding is pure CPU.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Callable

import pyray as pr

from . import config

# A world position is a plain (x, y, z) tuple, or the str name of an actor (whose
# live position is resolved each frame — so a tracking shot follows a mover).
Vec3 = tuple[float, float, float]
Target = "Vec3 | str"

# Roughly chest/head height of a scaled character — where the camera looks when
# it frames an actor, and where "look at this actor" resolves to.
LOOK_HEIGHT = config.CHARACTER_NATIVE_HEIGHT * config.CHARACTER_SCALE * 0.82


# --------------------------------------------------------------------------- #
# tiny vector + easing helpers (kept local so this module stands alone)
# --------------------------------------------------------------------------- #
def _add(a, b):  return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def _sub(a, b):  return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def _mul(a, s):  return (a[0] * s, a[1] * s, a[2] * s)
def _lerp(a, b, t): return a + (b - a) * t
def _lerp3(a, b, t): return (_lerp(a[0], b[0], t), _lerp(a[1], b[1], t), _lerp(a[2], b[2], t))


def _norm(v):
    m = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return (v[0] / m, v[1] / m, v[2] / m)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def ease(name: str, t: float) -> float:
    """Map linear progress [0,1] through an easing curve."""
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    if name == "linear":
        return t
    if name == "in":            # accelerate from rest (ease-in quad)
        return t * t
    if name == "out":           # decelerate to rest (ease-out quad)
        return 1.0 - (1.0 - t) * (1.0 - t)
    # default: smoothstep ease-in-out — the gentle dolly look
    return t * t * (3.0 - 2.0 * t)


# --------------------------------------------------------------------------- #
# Actors + blocking
# --------------------------------------------------------------------------- #
@dataclass
class _Beat:
    t: float                       # global start time (s)
    dur: float                     # duration (s); 0 for instantaneous (Face)
    kind: str                      # "move" | "anim" | "face"
    anim: str = config.ANIM_IDLE_NAME
    loop: bool = True
    ease: str = "inout"
    path: tuple = ()               # [(x, z), ...] waypoints for a move
    face: object = None            # Target to turn toward (face beats)

    @property
    def end(self) -> float:
        return self.t + self.dur


def Walk(t, dur, path, anim="Walk", ease="inout") -> _Beat:
    """Stroll along ground waypoints [(x,z), ...] over `dur`, facing the heading
    and playing a looping locomotion clip (Walk / Run)."""
    return _Beat(t, dur, "move", anim=anim, loop=True, ease=ease, path=tuple(path))


def Hold(t, dur, anim="Idle") -> _Beat:
    """Stand in place playing a looping clip (Idle by default)."""
    return _Beat(t, dur, "anim", anim=anim, loop=True)


def Play(t, dur, anim, loop=False) -> _Beat:
    """Play a one-shot clip (e.g. Victory, SitDown) and hold its last frame."""
    return _Beat(t, dur, "anim", anim=anim, loop=loop)


def Face(t, target) -> _Beat:
    """Instantly turn to face an actor name or a world point, and stay turned."""
    return _Beat(t, 0.0, "face", face=target)


def _along_path(path, p: float) -> tuple[float, float, float]:
    """Sample a polyline at progress p ∈ [0,1]; return (x, z, heading_degrees)."""
    if len(path) == 1:
        return path[0][0], path[0][1], 0.0
    segs = len(path) - 1
    fp = max(0.0, min(1.0, p)) * segs
    i = min(segs - 1, int(fp))
    lt = fp - i
    (x0, z0), (x1, z1) = path[i], path[i + 1]
    x = _lerp(x0, x1, lt)
    z = _lerp(z0, z1, lt)
    heading = math.degrees(math.atan2(x1 - x0, z1 - z0))   # +z forward, +x right
    return x, z, heading


@dataclass
class Actor:
    """A cutscene performer: a Character plus a timeline of beats. Looks
    (skin/hair/eyes/outfit) are applied by the demo via roster.apply_look so the
    model doesn't render as raw near-black materials."""
    name: str
    ch: object                                  # entities.Character
    beats: list = field(default_factory=list)

    def _latest(self, now: float, kinds: tuple) -> "_Beat | None":
        best = None
        for b in self.beats:
            if b.kind in kinds and b.t <= now and (best is None or b.t >= best.t):
                best = b
        return best

    def pose(self, now: float, resolve: Callable) -> None:
        """Set the Character's position / facing / animation frame for time `now`.
        Frames are derived directly from the clock, so the render is deterministic
        and reproducible regardless of machine speed."""
        c = self.ch

        # --- position (+ heading while walking) ---------------------------
        move = self._latest(now, ("move",))
        heading = None
        if move is not None and move.path:
            p = 1.0 if move.dur <= 0 else (now - move.t) / move.dur
            x, z, heading = _along_path(move.path, ease(move.ease, p))
            c.x, c.z = x, z

        # --- facing: a later Face beat overrides the walk heading ----------
        face = self._latest(now, ("face",))
        if face is not None and (move is None or face.t >= move.t):
            fx, _fy, fz = resolve(face.face)
            c.yaw = math.degrees(math.atan2(fx - c.x, fz - c.z))
        elif heading is not None:
            c.yaw = heading

        # --- animation clip + deterministic frame -------------------------
        anim = self._latest(now, ("move", "anim"))
        if anim is not None:
            c.anim_name = anim.anim
            c.anim_loop = anim.loop
            base = now if anim.loop else (now - anim.t)
            c._frame = max(0.0, base) * config.ANIM_FPS

    def end_time(self) -> float:
        return max((b.end for b in self.beats), default=0.0)


# --------------------------------------------------------------------------- #
# Camera shots
# --------------------------------------------------------------------------- #
@dataclass
class Shot:
    """One continuous camera take. `sampler(p01, resolve)` returns
    (position, look_at, fov, roll_degrees) for eased progress p01 ∈ [0,1]."""
    t: float
    dur: float
    sampler: Callable
    ease: str = "inout"
    caption: object = None          # optional (speaker, line) shown during the shot

    @property
    def end(self) -> float:
        return self.t + self.dur

    def sample(self, now: float, resolve: Callable):
        p = 1.0 if self.dur <= 0 else (now - self.t) / self.dur
        return self.sampler(ease(self.ease, p), resolve)

    # -- builders: the GTA cutscene vocabulary -----------------------------
    @classmethod
    def static(cls, t, dur, pos, look, fov=45.0, roll=0.0, **kw):
        """Locked-off tripod shot."""
        def s(p, rv):
            return rv(pos, raw=True), _at_height(rv(look), look), fov, roll
        return cls(t, dur, s, **kw)

    @classmethod
    def dolly(cls, t, dur, frm, to, look, fov=45.0, roll=0.0, ease="inout", **kw):
        """Straight push-in / pull-out between two camera points. `fov` may be a
        (start, end) pair to add a lens zoom; `look` can track an actor."""
        f0, f1 = (fov, fov) if isinstance(fov, (int, float)) else fov
        r0, r1 = (roll, roll) if isinstance(roll, (int, float)) else roll
        def s(p, rv):
            pos = _lerp3(rv(frm, raw=True), rv(to, raw=True), p)
            return pos, _at_height(rv(look), look), _lerp(f0, f1, p), _lerp(r0, r1, p)
        return cls(t, dur, s, ease=ease, **kw)

    @classmethod
    def orbit(cls, t, dur, center, radius, height, deg=(0.0, 90.0),
              look_height=LOOK_HEIGHT, fov=45.0, roll=0.0, ease="inout", **kw):
        """Arc around a point/actor — the classic reveal. `deg` is the start/end
        orbit angle; the camera rides a circle at `radius`/`height`."""
        d0, d1 = deg
        def s(p, rv):
            cx, cy, cz = rv(center)
            a = math.radians(_lerp(d0, d1, p))
            pos = (cx + math.sin(a) * radius, cy + height, cz + math.cos(a) * radius)
            return pos, (cx, cy + look_height, cz), fov, roll
        return cls(t, dur, s, ease=ease, **kw)

    @classmethod
    def crane(cls, t, dur, x, z, y, look, fov=45.0, roll=0.0, ease="inout", **kw):
        """Vertical move at a fixed (x, z): `y` is a (low, high) pair."""
        y0, y1 = y
        def s(p, rv):
            return (x, _lerp(y0, y1, p), z), _at_height(rv(look), look), fov, roll
        return cls(t, dur, s, ease=ease, **kw)

    @classmethod
    def track(cls, t, dur, target, offset, look=None, fov=45.0, roll=0.0, **kw):
        """Rigid follow: camera sits at the moving target + offset and stays
        locked on it (or on `look`). Re-reads the target every frame."""
        def s(p, rv):
            base = rv(target)
            return _add(base, offset), _at_height(rv(look or target), look or target), fov, roll
        return cls(t, dur, s, **kw)


def _at_height(resolved_pos, original_target):
    """When a look target is an *actor* (str), aim at its head, not its feet."""
    if isinstance(original_target, str):
        x, y, z = resolved_pos
        return (x, y + LOOK_HEIGHT, z)
    return resolved_pos


# --------------------------------------------------------------------------- #
# Scene + Director
# --------------------------------------------------------------------------- #
@dataclass
class Caption:
    t: float
    dur: float
    line: str
    speaker: str = ""

    @property
    def end(self) -> float:
        return self.t + self.dur


@dataclass
class Line:
    """One line of the script track. `kind` decides how it's drawn:
      - "say"     → character dialogue: a named speaker, lower-third subtitle.
      - "narrate" → voiceover: no speaker, centered near the top, dimmer.
    Narration and dialogue can overlap (a narrator under a character line)."""
    t: float
    dur: float
    text: str
    speaker: str = ""
    kind: str = "say"

    @property
    def end(self) -> float:
        return self.t + self.dur


def Say(t, dur, speaker, text) -> Line:
    """A line of character dialogue (shown as a lower-third subtitle)."""
    return Line(t, dur, text, speaker=speaker, kind="say")


def Narrate(t, dur, text) -> Line:
    """A line of narrator voiceover (shown centered near the top)."""
    return Line(t, dur, text, kind="narrate")


@dataclass
class Scene:
    """A complete cutscene: who's in it, the camera shot list, captions, and the
    output settings. World rendering is supplied as a callback by the runner, so
    this engine never has to know about Scene vs Park."""
    actors: list                       # list[Actor]
    shots: list                        # list[Shot]
    captions: list = field(default_factory=list)
    script: list = field(default_factory=list)   # list[Line]: narration + dialogue
    time_of_day: str = "Afternoon"
    resolution: tuple = (1920, 1080)
    fps: int = 60
    letterbox: float = 0.12            # fraction of height for each black bar (0 = off)
    music: str | None = None           # audio laid under the whole cut by MoviePy
    name: str = "cutscene"

    def total(self) -> float:
        ends = [s.end for s in self.shots] + [c.end for c in self.captions]
        ends += [a.end_time() for a in self.actors] + [ln.end for ln in self.script]
        return max(ends, default=0.0)


class Director:
    """Drives one Scene over a global clock and exposes the active camera."""

    def __init__(self, scene: Scene) -> None:
        self.scene = scene
        self.t = 0.0
        self.total = scene.total()
        self._actors = {a.name: a for a in scene.actors}
        self._shots = sorted(scene.shots, key=lambda s: s.t)
        self.camera = pr.Camera3D(pr.Vector3(0, 2, 6), pr.Vector3(0, 1, 0),
                                  pr.Vector3(0, 1, 0), 45.0, pr.CAMERA_PERSPECTIVE)
        self.caption = None            # (speaker, line) active dialogue, or None
        self.narration = None          # active narrator line (str), or None

    @property
    def done(self) -> bool:
        return self.t >= self.total

    def resolve(self, target, raw: bool = False):
        """Turn a Target into a world (x, y, z). `raw` keeps a tuple as-is; an
        actor name resolves to its live ground position."""
        if isinstance(target, str):
            a = self._actors.get(target)
            if a is None:
                return (0.0, 0.0, 0.0)
            return (a.ch.x, a.ch.y, a.ch.z)
        return tuple(target)

    def _active_shot(self) -> "Shot | None":
        active = None
        for s in self._shots:
            if s.t <= self.t < s.end:
                active = s
        return active or (self._shots[-1] if self._shots else None)

    def update(self, dt: float) -> None:
        # Pose every actor first so camera tracking reads fresh positions.
        for a in self.scene.actors:
            a.pose(self.t, self.resolve)

        shot = self._active_shot()
        if shot is not None:
            pos, look, fov, roll = shot.sample(self.t, self.resolve)
            self.camera.position = pr.Vector3(*pos)
            self.camera.target = pr.Vector3(*look)
            self.camera.up = pr.Vector3(*_roll_up(pos, look, roll))
            self.camera.fovy = float(fov)

        # Dialogue + narration: a shot-attached caption first (back-compat), then
        # the standalone caption track, then the richer script track (Say/Narrate).
        self.caption = None
        self.narration = None
        if shot is not None and shot.caption is not None and shot.t <= self.t < shot.end:
            self.caption = shot.caption
        for c in self.scene.captions:
            if c.t <= self.t < c.end:
                self.caption = (c.speaker, c.line)
        for ln in self.scene.script:
            if ln.t <= self.t < ln.end:
                if ln.kind == "narrate":
                    self.narration = ln.text
                else:
                    self.caption = (ln.speaker, ln.text)

        self.t += dt


def _roll_up(pos, look, roll_deg: float):
    """Camera up-vector, tilted by `roll_deg` around the view axis (dutch angle)."""
    f = _norm(_sub(look, pos))
    right = _norm(_cross(f, (0.0, 1.0, 0.0)))
    up = _cross(right, f)
    if abs(roll_deg) < 1e-4:
        return up
    r = math.radians(roll_deg)
    return _add(_mul(up, math.cos(r)), _mul(right, math.sin(r)))


# --------------------------------------------------------------------------- #
# Recorder — off-screen render + clean overlay + PNG frame dump
# --------------------------------------------------------------------------- #
_CAP_BG = pr.Color(0, 0, 0, 0)
_CAP_FG = pr.Color(236, 236, 240, 255)
_CAP_SPEAKER = pr.Color(120, 200, 255, 255)
_NARR_FG = pr.Color(214, 206, 188, 235)        # warm, dimmer voiceover


class Recorder:
    """Renders the director's world to an off-screen texture with cinematic
    letterbox + captions (no HUD), and writes each frame to disk.

    `draw_world(camera)` is the caller's hook — it should draw the 3D world for
    the given camera (e.g. `scene.draw_world(chars, registry, camera, None)` or
    `park.draw(camera, season)`), which already opens/closes 3D mode itself."""

    def __init__(self, scene: Scene, sky_color, out_dir: str = "recordings") -> None:
        self.scene = scene
        self.w, self.h = scene.resolution
        self.sky = sky_color
        self.rt = pr.load_render_texture(self.w, self.h)
        self.dir = os.path.join(out_dir, scene.name)
        os.makedirs(self.dir, exist_ok=True)
        self._n = 0
        self.frame_paths: list[str] = []

    def render(self, director: Director, draw_world: Callable) -> None:
        pr.begin_texture_mode(self.rt)
        pr.clear_background(self.sky)
        draw_world(director.camera)                # opens + closes 3D mode itself
        self._letterbox()
        self._narration(director.narration)
        self._dialogue(director.caption)
        pr.end_texture_mode()

    def save_frame(self) -> str:
        """Write the current render texture to the next PNG in the sequence."""
        self._n += 1
        path = os.path.join(self.dir, f"frame_{self._n:05d}.png")
        img = pr.load_image_from_texture(self.rt.texture)
        pr.image_flip_vertical(img)                # render textures are stored bottom-up
        pr.export_image(img, path)
        pr.unload_image(img)
        self.frame_paths.append(path)
        return path

    def blit_to_screen(self) -> None:
        """For --live preview: draw the render texture to the window, y-flipped."""
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        src = pr.Rectangle(0, 0, self.w, -self.h)
        dst = pr.Rectangle(0, 0, sw, sh)
        pr.draw_texture_pro(self.rt.texture, src, dst, pr.Vector2(0, 0), 0.0, pr.WHITE)

    def unload(self) -> None:
        pr.unload_render_texture(self.rt)

    # -- overlay -----------------------------------------------------------
    def _letterbox(self) -> None:
        if self.scene.letterbox <= 0:
            return
        bar = int(self.h * self.scene.letterbox)
        black = pr.Color(0, 0, 0, 255)
        pr.draw_rectangle(0, 0, self.w, bar, black)
        pr.draw_rectangle(0, self.h - bar, self.w, bar, black)

    def _wrap(self, text: str, fs: int, max_w: int) -> list[str]:
        """Greedy word-wrap to fit `max_w` pixels at font size `fs`."""
        lines, cur = [], ""
        for w in text.split(" "):
            trial = (cur + " " + w).strip()
            if cur and pr.measure_text(trial, fs) > max_w:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    def _draw_centered(self, text: str, y: int, fs: int, color) -> None:
        tw = pr.measure_text(text, fs)
        x = (self.w - tw) // 2
        pr.draw_text(text, x + 2, y + 2, fs, pr.Color(0, 0, 0, 180))   # shadow
        pr.draw_text(text, x, y, fs, color)

    def _narration(self, text) -> None:
        """Narrator voiceover: centered just under the top letterbox bar."""
        if not text:
            return
        fs = max(18, self.h // 34)
        bar = int(self.h * self.scene.letterbox)
        y = (bar + 14) if bar else 28
        for ln in self._wrap(text, fs, int(self.w * 0.7)):
            self._draw_centered(ln, y, fs, _NARR_FG)
            y += fs + 8

    def _dialogue(self, caption) -> None:
        """Character dialogue: speaker name + wrapped line in the lower third."""
        if not caption:
            return
        speaker, line = caption
        fs = max(20, self.h // 30)
        bar = int(self.h * self.scene.letterbox)
        wrapped = self._wrap(line, fs, int(self.w * 0.72))
        block_h = len(wrapped) * (fs + 6)
        y = (self.h - bar - 18 - block_h) if bar else (self.h - 28 - block_h)
        if speaker:
            self._draw_centered(speaker, y - fs - 6, fs, _CAP_SPEAKER)
        for ln in wrapped:
            self._draw_centered(ln, y, fs, _CAP_FG)
            y += fs + 6


# --------------------------------------------------------------------------- #
# Encode — hand the PNG sequence to MoviePy (bundles its own ffmpeg)
# --------------------------------------------------------------------------- #
def encode(frame_paths: list[str], fps: int, out_path: str,
           music: str | None = None) -> str:
    """Stitch a PNG sequence into an .mp4 with MoviePy. Audio is optional and is
    trimmed/looped to the clip length. Imports MoviePy lazily so the engine has
    no hard dependency until you actually encode."""
    if not frame_paths:
        raise ValueError("no frames to encode")
    try:                                   # MoviePy 2.x layout
        from moviepy import ImageSequenceClip, AudioFileClip
        v2 = True
    except ImportError:                    # MoviePy 1.x layout
        from moviepy.editor import ImageSequenceClip, AudioFileClip
        v2 = False

    clip = ImageSequenceClip(frame_paths, fps=fps)
    if music and os.path.exists(music):
        audio = AudioFileClip(music)
        if v2:
            audio = audio.subclipped(0, min(audio.duration, clip.duration))
            clip = clip.with_audio(audio)
        else:
            audio = audio.subclip(0, min(audio.duration, clip.duration))
            clip = clip.set_audio(audio)
    clip.write_videofile(out_path, codec="libx264", audio_codec="aac",
                         fps=fps, logger=None)
    return out_path


# --------------------------------------------------------------------------- #
# Runner — the deterministic record loop (and an optional live preview)
# --------------------------------------------------------------------------- #
def record(scene: Scene, draw_world: Callable, sky_color, *,
           out_dir: str = "recordings", encode_video: bool = True) -> "str | None":
    """Step the Scene at a fixed dt, render every frame with no GUI, dump the PNG
    sequence, and (optionally) encode an .mp4. Returns the video path, or None if
    encoding was skipped or MoviePy isn't installed yet.

    Assumes a window/GL context is already initialised by the caller."""
    director = Director(scene)
    rec = Recorder(scene, sky_color, out_dir=out_dir)
    dt = 1.0 / scene.fps
    n_frames = int(math.ceil(scene.total() * scene.fps))
    for _ in range(n_frames):
        director.update(dt)
        rec.render(director, draw_world)
        rec.save_frame()
    print(f"[cinematic] wrote {len(rec.frame_paths)} frames to {rec.dir}")

    video = None
    if encode_video:
        out = os.path.join(out_dir, f"{scene.name}.mp4")
        try:
            video = encode(rec.frame_paths, scene.fps, out, music=scene.music)
            print(f"[cinematic] encoded {video}")
        except ImportError:
            print("[cinematic] MoviePy not installed — frames kept; "
                  "run `pip install moviepy` then re-encode.")
    rec.unload()
    return video

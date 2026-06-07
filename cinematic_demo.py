"""A worked example cutscene — drive game/cinematic.py against the office scene.

This is what "authoring a scene" looks like: place a couple of actors, give each
a timeline of beats, lay out a list of camera shots, press record. No GUI is
drawn — just the world, letterbox bars, and captions — so the output reads as a
GTA-style cutscene rather than gameplay.

    # final, deterministic render → recordings/office_intro.mp4
    python cinematic_demo.py

    # realtime preview while you tune angles (no files written)
    python cinematic_demo.py --live

    # render the frames but skip the MoviePy encode (e.g. before you've installed it)
    python cinematic_demo.py --no-encode

raylib needs a GL context to render, so this needs a display (a desktop is fine;
a headless box needs xvfb). The MoviePy encode is pure CPU and runs anywhere.
"""
from __future__ import annotations

import argparse

import pyray as pr

from game import config, roster, cinematic
from game import scene as scene_mod
from game import floorplan
from game.assets import ModelRegistry
from game.daylight import DayCycle
from game.entities import Character
from game.cinematic import Actor, Shot, Caption, Scene, Walk, Hold, Play, Face


def build_scene() -> tuple[Scene, list[Character]]:
    """The cutscene script. The CEO strolls into the new HQ; the lead agent turns
    and they share the 'we're live' beat. The camera does establishing → tracking
    → orbit reveal → push-in, like a four-shot opening cinematic."""
    # --- actors --------------------------------------------------------------
    ceo_ch = Character(name="You (CEO)", role="CEO", x=3.5, z=5.5,
                       color=pr.GOLD, model="Casual_Male.gltf")
    roster.apply_look(ceo_ch, {"skin_idx": 2, "hair_idx": 5, "eye_idx": 1})

    agent_ch = Character(name="Ada", role="Engineer", x=-1.6, z=-0.5,
                         color=pr.SKYBLUE, model="Casual2_Female.gltf")
    roster.apply_look(agent_ch, {"skin_idx": 4, "hair_idx": 2, "eye_idx": 3})

    ceo = Actor("ceo", ceo_ch, beats=[
        Hold(0.0, 1.0, "Idle"),                                  # pause in the doorway
        Walk(1.0, 4.0, [(3.5, 5.5), (1.8, 3.0), (0.5, 1.0)]),    # stroll in
        Hold(5.0, 3.2, "Idle"),                                  # take in the room
        Face(8.2, "agent"),                                      # turn to Ada
        Play(8.6, 3.4, "Victory"),                               # "we're live"
    ])
    agent = Actor("agent", agent_ch, beats=[
        Hold(0.0, 8.2, "Idle"),
        Face(8.2, "ceo"),
        Play(8.6, 3.4, "Victory"),
    ])

    # --- camera shot list ----------------------------------------------------
    shots = [
        Shot.static(0.0, 3.2, pos=(7.0, 6.5, 8.0), look=(0.5, 1.0, 1.5),
                    fov=42.0, caption=("", "A NEW VENTURE")),
        Shot.track(3.2, 3.0, target="ceo", offset=(2.6, 1.7, 3.2),
                   look="ceo", fov=38.0),
        Shot.orbit(6.2, 3.6, center="ceo", radius=4.0, height=1.8,
                   deg=(25.0, 165.0), fov=37.0),
        Shot.dolly(9.8, 2.6, frm=(0.5, 1.7, 3.6), to=(0.5, 1.6, 2.1),
                   look="ceo", fov=(40.0, 30.0), roll=(0.0, -4.0),
                   caption=("CEO", "We're live.")),
    ]

    captions = [Caption(0.4, 2.4, "Day one.", speaker="")]

    scene = Scene(actors=[ceo, agent], shots=shots, captions=captions,
                  time_of_day="Dusk", resolution=(1920, 1080), fps=60,
                  letterbox=0.12, music=None, name="office_intro")
    return scene, [ceo_ch, agent_ch]


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a no-GUI cinematic cutscene.")
    ap.add_argument("--live", action="store_true",
                    help="realtime preview in a window (writes nothing)")
    ap.add_argument("--no-encode", action="store_true",
                    help="dump PNG frames but skip the MoviePy .mp4 encode")
    ap.add_argument("--width", type=int, default=0, help="override output width")
    ap.add_argument("--height", type=int, default=0, help="override output height")
    args = ap.parse_args()

    scene, chars = build_scene()
    if args.width and args.height:
        scene.resolution = (args.width, args.height)

    # --- window / GL context -------------------------------------------------
    flags = pr.FLAG_MSAA_4X_HINT
    if not args.live:
        flags |= pr.FLAG_WINDOW_HIDDEN          # offline: render to texture, no visible window
    pr.set_config_flags(flags)
    win_w, win_h = (1280, 720) if args.live else (320, 180)
    pr.init_window(win_w, win_h, "Company.AI — Cinematic")
    pr.set_target_fps(scene.fps)
    pr.rl_set_clip_planes(0.5, 250.0)           # match the game's depth range

    # --- world -------------------------------------------------------------
    registry = ModelRegistry()
    day = DayCycle(start=scene.time_of_day)
    registry.set_daylight(day)
    office = scene_mod.Scene(floorplan.DEFAULT_HQ)
    sky = day.sky_color()

    def draw_world(camera) -> None:
        # The "no GUI": only the 3D world is drawn — no HUD, buttons, or labels.
        office.draw_world(chars, registry, camera, None)

    try:
        if args.live:
            _preview(scene, draw_world, sky)
        else:
            cinematic.record(scene, draw_world, sky, encode_video=not args.no_encode)
    finally:
        registry.unload_all()
        pr.close_window()


def _preview(scene: Scene, draw_world, sky) -> None:
    """Realtime preview: play the cut in a window, looping, for tuning angles."""
    director = cinematic.Director(scene)
    rec = cinematic.Recorder(scene, sky, out_dir="recordings")
    while not pr.window_should_close():
        dt = pr.get_frame_time()
        if director.done:
            director.t = 0.0                    # loop the cut
        director.update(dt)
        rec.render(director, draw_world)
        pr.begin_drawing()
        pr.clear_background(pr.BLACK)
        rec.blit_to_screen()
        pr.draw_fps(10, 10)
        pr.end_drawing()
    rec.unload()


if __name__ == "__main__":
    main()

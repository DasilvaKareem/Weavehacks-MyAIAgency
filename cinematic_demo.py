"""Render the no-GUI cutscenes from game/cutscenes.py (GTA-style cinematics).

There are 10 cutscenes, one per flagship to-do in the quest line — each tagged
with when it plays (begin / middle / end of that to-do). Authoring lives in
game/cutscenes.py; this file is just the runner: it stands up a window + the
office world and films whichever scene you pick.

    python cinematic_demo.py --list             # show the 10 cutscenes
    python cinematic_demo.py --todo series_a     # render one → recordings/10_series_a.mp4
    python cinematic_demo.py --all               # render all 10
    python cinematic_demo.py --todo office --live # realtime preview (writes nothing)
    python cinematic_demo.py --todo mvp --no-encode  # dump PNG frames, skip the encode

raylib needs a GL context to render, so this needs a display (a desktop is fine;
a headless box needs xvfb). The MoviePy encode is pure CPU and runs anywhere.
"""
from __future__ import annotations

import argparse

import pyray as pr

from game import cinematic, cutscenes
from game import scene as scene_mod
from game import floorplan
from game.assets import ModelRegistry
from game.daylight import DayCycle


def main() -> None:
    ap = argparse.ArgumentParser(description="Render no-GUI cinematic cutscenes.")
    ap.add_argument("--todo", default="name",
                    help="which to-do's cutscene to render (see --list)")
    ap.add_argument("--all", action="store_true", help="render all 10 cutscenes")
    ap.add_argument("--list", action="store_true", help="list the cutscenes and exit")
    ap.add_argument("--live", action="store_true",
                    help="realtime preview in a window (writes nothing)")
    ap.add_argument("--no-encode", action="store_true",
                    help="dump PNG frames but skip the MoviePy .mp4 encode")
    ap.add_argument("--width", type=int, default=0, help="override output width")
    ap.add_argument("--height", type=int, default=0, help="override output height")
    args = ap.parse_args()

    if args.list:
        for key in cutscenes.ORDER:
            c = cutscenes.CUTSCENES[key]
            print(f"  {key:<10} [{c.trigger:^6}] {c.title}")
        return

    keys = cutscenes.ORDER if args.all else [args.todo]
    for k in keys:
        if k not in cutscenes.CUTSCENES:
            raise SystemExit(f"unknown to-do '{k}'. Try --list.")

    # --- window / GL context (once) -----------------------------------------
    flags = pr.FLAG_MSAA_4X_HINT
    if not args.live:
        flags |= pr.FLAG_WINDOW_HIDDEN          # offline: render to texture, no visible window
    pr.set_config_flags(flags)
    win_w, win_h = (1280, 720) if args.live else (320, 180)
    pr.init_window(win_w, win_h, "Company.AI — Cinematic")
    pr.set_target_fps(60)
    pr.rl_set_clip_planes(0.5, 250.0)           # match the game's depth range

    registry = ModelRegistry()
    office = scene_mod.Scene(floorplan.DEFAULT_HQ)

    try:
        for key in keys:
            scene, chars = cutscenes.build(key)
            if args.width and args.height:
                scene.resolution = (args.width, args.height)
            day = DayCycle(start=scene.time_of_day)
            registry.set_daylight(day)
            sky = day.sky_color()

            def draw_world(camera, _chars=chars) -> None:
                # The "no GUI": only the 3D world is drawn — no HUD, no labels.
                office.draw_world(_chars, registry, camera, None)

            print(f"[cinematic] {key}: {scene.name}  ({scene.total():.1f}s)")
            if args.live:
                _preview(scene, draw_world, sky)
            else:
                cinematic.record(scene, draw_world, sky, encode_video=not args.no_encode)
    finally:
        registry.unload_all()
        pr.close_window()


def _preview(scene, draw_world, sky) -> None:
    """Realtime preview: play the cut in a window, looping, for tuning angles."""
    director = cinematic.Director(scene)
    rec = cinematic.Recorder(scene, sky, out_dir="recordings")
    while not pr.window_should_close():
        dt = pr.get_frame_time()
        if director.done:
            director.t = 0.0
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

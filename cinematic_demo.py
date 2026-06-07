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
import json
import os

import pyray as pr

from game import cinematic, cutscenes, cast, films
from game import scene as scene_mod
from game import floorplan
from game.assets import ModelRegistry
from game.daylight import DayCycle


def main() -> None:
    try:                                  # the TTS voice track needs GEMINI_API_KEY
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description="Render no-GUI cinematic cutscenes.")
    ap.add_argument("--todo", default="name",
                    help="which to-do's cutscene to render (see --list)")
    ap.add_argument("--all", action="store_true", help="render all 10 cutscenes")
    ap.add_argument("--demo", action="store_true",
                    help="render the 3-beat 'how it works' reel (chaos → redis → weave)")
    ap.add_argument("--list", action="store_true", help="list the cutscenes and exit")
    ap.add_argument("--live", action="store_true",
                    help="realtime preview in a window (writes nothing)")
    ap.add_argument("--no-encode", action="store_true",
                    help="dump PNG frames but skip the MoviePy .mp4 encode")
    ap.add_argument("--width", type=int, default=0, help="override output width")
    ap.add_argument("--height", type=int, default=0, help="override output height")
    ap.add_argument("--generic", action="store_true",
                    help="use stock actors instead of your saved CEO + hired agents")
    ap.add_argument("--no-voice", action="store_true",
                    help="skip the per-agent Gemini TTS voice track (silent cut)")
    ap.add_argument("--film", default="",
                    help="render a Film Director's spec (drive path like "
                         "/films/x.json, or a local .json file)")
    args = ap.parse_args()

    if args.list:
        print("quest line:")
        for key in cutscenes.ORDER:
            c = cutscenes.CUTSCENES[key]
            print(f"  {key:<10} [{c.trigger:^6}] {c.title}")
        print("demo reel (--demo):")
        for key in cutscenes.DEMO_ORDER:
            c = cutscenes.CUTSCENES[key]
            print(f"  {key:<10} [{c.trigger:^6}] {c.title}")
        return

    # Cast from the active company's save (real CEO + hires) unless --generic.
    ceo_profile, agents = None, []
    if not args.generic:
        try:
            from backend.store import AgentStore
            store = AgentStore()
            raw = store.get_setting("ceo_profile")
            ceo_profile = json.loads(raw) if raw else None
            agents = store.list_agents()
            who = ceo_profile.get("name", "CEO") if ceo_profile else "CEO"
            print(f"[cast] {who} + {len(agents)} hired agent(s) from your save")
        except Exception as exc:
            print(f"[cast] no save loaded ({exc}); using stock actors")

    if args.all:
        keys = cutscenes.ORDER
    elif args.demo:
        keys = cutscenes.DEMO_ORDER
    else:
        keys = [args.todo]
    if not args.film:
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
        if args.film:
            spec = _load_film_spec(args.film)
            jobs = [(spec.get("title", "film"), *films.build_film(spec))]
        else:
            jobs = [(key, *cutscenes.build(key)) for key in keys]

        for label, scene, chars in jobs:
            if args.width and args.height:
                scene.resolution = (args.width, args.height)
            # Recast with the real CEO + hires (names/looks/models), then synth a
            # per-agent voice track. Skipped for --generic / --no-voice / --live.
            voices = cast.recast(scene, ceo_profile, agents) if not args.generic else {}
            if not args.live and not args.no_voice:
                os.makedirs("recordings", exist_ok=True)
                vpath = os.path.join("recordings", f"{scene.name}_voice.wav")
                try:
                    track = cast.voice_track(scene, voices, vpath)
                    if track:
                        scene.music = track
                        cast.fit_shots(scene)   # re-timed lines may run past the shots
                        print(f"[cast] voiced {scene.name} -> {track} ({scene.total():.1f}s)")
                except Exception as exc:
                    print(f"[cast] voice track skipped ({exc})")
            day = DayCycle(start=scene.time_of_day)
            registry.set_daylight(day)
            sky = day.sky_color()

            def draw_world(camera, _chars=chars) -> None:
                # The "no GUI": only the 3D world is drawn — no HUD, no labels.
                office.draw_world(_chars, registry, camera, None)

            print(f"[cinematic] {label}: {scene.name}  ({scene.total():.1f}s)")
            if args.live:
                _preview(scene, draw_world, sky)
            else:
                cinematic.record(scene, draw_world, sky, encode_video=not args.no_encode)
    finally:
        registry.unload_all()
        pr.close_window()


def _load_film_spec(path: str) -> dict:
    """Load a film spec from the company drive (a virtual path) or a local file."""
    try:
        from backend.store import AgentStore
        row = AgentStore().fs_get("/" + path.lstrip("/"))
        if row is not None and row.content:
            return json.loads(row.content)
    except Exception:
        pass
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    raise SystemExit(f"film spec not found: {path}")


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

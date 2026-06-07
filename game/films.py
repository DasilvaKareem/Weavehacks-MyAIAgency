"""Turn a Film Director's spec (backend/film_tools.py) into a renderable Scene.

A film spec is high-level on purpose — a cast (by role) and a timed script — so the
LLM can't author broken camera work. build_film() lays the cast out (CEO front and
centre, the team in a row behind) and AUTO-GENERATES a valid, cinematic shot list
(establishing orbit, then cycling push-ins / orbits / cranes across the cast) timed
to the script. The render path then recasts it with the real CEO + hires and voices
each line (game/cast.py), exactly like the built-in cutscenes.
"""
from __future__ import annotations

from . import cutscenes
from .cinematic import Scene, Shot, Actor, Hold, Say, Narrate


def _seed(label: str, i: int) -> int:
    return (sum(ord(c) for c in label) * 7 + i * 13) & 0xFFFF


def build_film(spec: dict):
    """spec -> (Scene, chars). chars is the list of Characters for draw_world."""
    cast = spec.get("cast") or [{"role": "CEO", "label": "CEO"}]
    ceo_entry = next((c for c in cast if c.get("role") == "CEO"),
                     {"role": "CEO", "label": "CEO"})
    ceo_orig = ceo_entry.get("label", "CEO")
    others = [c for c in cast if c.get("role") != "CEO"]

    # --- script (remap the CEO's label to the canonical "CEO") --------------
    script, positions = [], {"CEO": (0.0, 0.0)}
    for ln in spec.get("script") or []:
        text = (ln.get("text") or "").strip()
        if not text:
            continue
        if ln.get("kind") == "narrate":
            script.append(Narrate(float(ln.get("t", 0.0)), float(ln.get("dur", 3.0)), text))
        else:
            spk = ln.get("speaker", "")
            if spk == ceo_orig:
                spk = "CEO"
            script.append(Say(float(ln.get("t", 0.0)), float(ln.get("dur", 3.0)), spk, text))
    total = max([l.end for l in script], default=10.0) + 0.8

    # --- place the cast: CEO at origin, the team in a row just behind -------
    actors = []
    chars = []
    ceo = cutscenes._ceo(0.0, 0.0, face=(0.0, 8.0))
    actors.append(Actor("CEO", ceo, beats=[Hold(0.0, total, "Idle")]))
    chars.append(ceo)
    n = len(others)
    for i, c in enumerate(others):
        label, role = c.get("label") or c.get("role", "?"), c.get("role", "")
        x = (i - (n - 1) / 2.0) * 1.6
        z = -1.9
        ch = cutscenes._person(label, x, z, seed=_seed(label, i), role=role)
        cutscenes._face(ch, (x, 8.0))            # face the camera/front
        actors.append(Actor(label, ch, beats=[Hold(0.0, total, "Idle")]))
        chars.append(ch)
        positions[label] = (x, z)

    # --- auto shot list: establish, then cycle coverage across the cast -----
    shots = [Shot.orbit(0.0, min(4.0, max(2.5, total * 0.28)),
                        center="CEO", radius=6.5, height=3.2, deg=(-38, 22))]
    targets = ["CEO"] + [c.get("label") or c.get("role") for c in others]
    t, i = shots[0].end, 0
    SEG = 3.6
    while t < total - 0.05:
        dur = min(SEG, total - t)
        tgt = targets[i % len(targets)]
        tx, tz = positions.get(tgt, (0.0, 0.0))
        mode = i % 3
        if mode == 0:                            # push-in on the target
            shots.append(Shot.dolly(t, dur, frm=(tx + 2.4, 2.5, tz + 5.0),
                                    to=(tx + 0.9, 1.75, tz + 2.6), look=tgt, fov=(42, 32)))
        elif mode == 1:                          # orbit the group
            a0 = (i * 35) % 360
            shots.append(Shot.orbit(t, dur, center="CEO", radius=5.6, height=2.8,
                                    deg=(a0, a0 + 65)))
        else:                                    # low crane up on the target
            shots.append(Shot.crane(t, dur, x=tx + 0.5, z=tz + 3.4, y=(1.2, 2.7), look=tgt))
        t += dur
        i += 1

    scene = Scene(actors=actors, shots=shots, script=script,
                  time_of_day=spec.get("time_of_day", "Afternoon"),
                  name=f"film_{spec.get('slug', 'film')}")
    return scene, chars

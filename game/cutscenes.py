"""A library of 10 cutscenes, one per flagship to-do in the quest line.

Each entry is a `CutsceneDef`: the to-do `key` it belongs to, a `trigger`
saying *when* in that to-do it should play (begin / middle / end), and a builder
that returns a ready-to-record `cinematic.Scene` plus the Characters it animates.

The to-dos themselves live in game/tasks.py; these are the cinematic punctuation
for the big ones — naming the company, winning over Robin, the seed check, the
first office, the first hire, launch day, and the Series A win. They're authored
in the office interior (the reliable standalone world); pointing one at the city
is just a different `draw_world` callback at record time — the engine doesn't
care.

    from game import cutscenes
    scene, chars = cutscenes.build("series_a")
    # ...hand (scene, chars) to the recorder in cinematic_demo.py
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable

import pyray as pr

from . import config, roster
from .entities import Character
from .cinematic import Actor, Shot, Caption, Scene, Walk, Hold, Play, Face

# A spread of office-appropriate models; missing files fall back to colored boxes,
# so this list is safe even if a pack is trimmed.
_MODELS = [
    "Casual_Male.gltf", "Casual2_Female.gltf", "Casual3_Male.gltf",
    "Casual_Female.gltf", "Casual2_Male.gltf", "Casual3_Female.gltf",
    "OldClassy_Male.gltf", "OldClassy_Female.gltf", "Casual_Bald.gltf",
    "BlueSoldier_Female.gltf",
]


def _person(name: str, x: float, z: float, *, seed: int,
            model: str | None = None, role: str = "") -> Character:
    """A tinted Character at (x, z), seeded so its look is varied but reproducible."""
    m = model or _MODELS[seed % len(_MODELS)]
    ch = Character(name=name, role=role, x=x, z=z, color=pr.SKYBLUE, model=m)
    roster.apply_look(ch, roster.random_look(random.Random(seed * 7 + 13)))
    return ch


def _ring(n: int, radius: float, center=(0.0, 0.0), start_deg=0.0):
    """n evenly spaced (x, z) points on a circle — for crowd blocking."""
    out = []
    for i in range(n):
        a = math.radians(start_deg + 360.0 * i / n)
        out.append((center[0] + math.sin(a) * radius, center[1] + math.cos(a) * radius))
    return out


def _face_center(ch: Character, center=(0.0, 0.0)) -> None:
    ch.yaw = math.degrees(math.atan2(center[0] - ch.x, center[1] - ch.z))


# --------------------------------------------------------------------------- #
# the 10 cutscenes
# --------------------------------------------------------------------------- #
def _cut_name():
    """name · BEGIN — the founder alone in an empty room, before anything exists."""
    ceo = _person("You (CEO)", 0.0, 0.0, seed=1, model="Casual_Male.gltf", role="CEO")
    a = Actor("ceo", ceo, beats=[Hold(0, 11, "Idle"), Play(8.0, 3.0, "Victory")])
    shots = [
        Shot.crane(0.0, 4.0, x=0.0, z=4.6, y=(1.3, 4.2), look="ceo", fov=40.0,
                   caption=("", "CHAPTER ONE")),
        Shot.dolly(4.0, 3.6, frm=(0.0, 1.6, 4.6), to=(0.0, 1.6, 2.3), look="ceo",
                   fov=(42.0, 30.0), caption=("", "Name your company.")),
        Shot.static(7.6, 3.0, pos=(2.3, 1.7, 2.6), look="ceo", fov=34.0,
                    caption=("", "Every empire needs a name.")),
    ]
    return Scene([a], shots, time_of_day="Morning", name="01_name"), [ceo]


def _cut_cofounder():
    """cofounder · MIDDLE — Robin walks over and throws in with you."""
    ceo = _person("You (CEO)", -1.6, 0.6, seed=1, model="Casual_Male.gltf", role="CEO")
    robin = _person("Robin", 3.4, 0.6, seed=4, model="OldClassy_Female.gltf", role="Co-founder")
    ceo_a = Actor("ceo", ceo, beats=[
        Hold(0, 12, "Idle"), Face(4.6, "robin"), Play(7.2, 3.0, "Victory")])
    robin_a = Actor("robin", robin, beats=[
        Hold(0, 1.2, "Idle"),
        Walk(1.2, 3.2, [(3.4, 0.6), (0.4, 0.6)], "Walk"),
        Face(4.6, "ceo"), Hold(4.6, 7.4, "Idle")])
    shots = [
        Shot.static(0.0, 2.6, pos=(0.6, 1.7, 5.2), look="robin", fov=40.0,
                    caption=("", "Win over co-founder Robin")),
        Shot.track(2.6, 2.4, target="robin", offset=(-2.4, 1.6, 2.6), look="robin", fov=38.0),
        Shot.orbit(5.0, 3.4, center=(-0.6, 0.0, 0.6), radius=3.8, height=1.7,
                   deg=(200.0, 320.0), fov=36.0, caption=("ROBIN", "Alright. I'm in.")),
        Shot.dolly(8.4, 3.0, frm=(-0.6, 1.7, 3.4), to=(-0.6, 1.6, 2.2), look="ceo",
                   fov=(40.0, 31.0)),
    ]
    return Scene([ceo_a, robin_a], shots, time_of_day="Dusk", name="02_cofounder"), [ceo, robin]


def _cut_seed():
    """seed · END — the angel writes the first check."""
    ceo = _person("You (CEO)", -0.8, 1.0, seed=1, model="Casual_Male.gltf", role="CEO")
    angel = _person("Angel", 1.6, 1.0, seed=6, model="OldClassy_Male.gltf", role="Investor")
    ceo_a = Actor("ceo", ceo, beats=[
        Hold(0, 12, "Idle"), Face(0.2, "angel"), Play(6.6, 3.4, "Victory")])
    angel_a = Actor("angel", angel, beats=[
        Face(0, "ceo"), Hold(0, 4.0, "Idle"), Play(4.0, 2.2, "PickUp"), Hold(6.2, 6, "Idle")])
    shots = [
        Shot.orbit(0.0, 3.4, center=(0.4, 0.0, 1.0), radius=3.6, height=1.6,
                   deg=(40.0, 150.0), fov=37.0, caption=("", "Raise your seed money")),
        Shot.dolly(3.4, 3.2, frm=(1.6, 1.7, 4.2), to=(1.6, 1.5, 2.6), look="angel",
                   fov=(40.0, 32.0), caption=("ANGEL", "Let's make it official.")),
        Shot.dolly(6.6, 3.4, frm=(-0.8, 1.7, 3.4), to=(-0.8, 1.6, 2.1), look="ceo",
                   fov=(40.0, 29.0), roll=(0.0, -3.0), caption=("", "+ $10,000 seed")),
    ]
    return Scene([ceo_a, angel_a], shots, time_of_day="Afternoon", name="03_seed"), [ceo, angel]


def _cut_office():
    """office · BEGIN — you walk into the empty office you just leased."""
    ceo = _person("You (CEO)", 4.0, 5.6, seed=1, model="Casual_Male.gltf", role="CEO")
    ceo_a = Actor("ceo", ceo, beats=[
        Hold(0, 1.0, "Idle"),
        Walk(1.0, 4.0, [(4.0, 5.6), (1.6, 2.8), (0.0, 0.6)], "Walk"),
        Hold(5.0, 2.4, "Idle"), Play(7.4, 3.2, "Victory")])
    shots = [
        Shot.static(0.0, 3.0, pos=(6.8, 6.2, 7.6), look=(0.0, 1.0, 1.5), fov=44.0,
                    caption=("", "Lease your first office")),
        Shot.track(3.0, 2.6, target="ceo", offset=(2.4, 1.7, 3.0), look="ceo", fov=38.0),
        Shot.orbit(5.6, 3.0, center="ceo", radius=3.8, height=1.8, deg=(20.0, 150.0),
                   fov=37.0),
        Shot.crane(8.6, 2.4, x=0.0, z=3.4, y=(1.6, 3.6), look="ceo", fov=36.0,
                   caption=("", "Home.")),
    ]
    return Scene([ceo_a], shots, time_of_day="Morning", name="04_office"), [ceo]


def _cut_intern():
    """intern · MIDDLE — your first intern jogs in, eager to start."""
    ceo = _person("You (CEO)", -0.6, 0.4, seed=1, model="Casual_Male.gltf", role="CEO")
    intern = _person("Intern", 4.2, -3.0, seed=2, model="Casual2_Male.gltf", role="Intern")
    ceo_a = Actor("ceo", ceo, beats=[Hold(0, 11, "Idle"), Face(4.4, "intern")])
    intern_a = Actor("intern", intern, beats=[
        Hold(0, 0.8, "Idle"),
        Walk(0.8, 3.4, [(4.2, -3.0), (1.8, -0.6), (1.0, 0.4)], "Run"),
        Face(4.4, "ceo"), Play(4.6, 2.6, "Victory"), Hold(7.2, 4, "Idle")])
    shots = [
        Shot.static(0.0, 2.4, pos=(2.4, 1.6, -5.0), look="intern", fov=40.0,
                    caption=("", "Take on your first intern")),
        Shot.track(2.4, 2.6, target="intern", offset=(-2.2, 1.6, -2.4), look="intern", fov=37.0),
        Shot.orbit(5.0, 3.2, center=(0.2, 0.0, 0.4), radius=3.6, height=1.7,
                   deg=(150.0, 30.0), fov=36.0, caption=("INTERN", "Where do I start?")),
        Shot.dolly(8.2, 2.8, frm=(-0.6, 1.7, 3.0), to=(-0.6, 1.6, 2.0), look="ceo", fov=(40, 31)),
    ]
    return Scene([ceo_a, intern_a], shots, time_of_day="Noon", name="05_intern"), [ceo, intern]


def _cut_engineer():
    """engineer · END — your first engineer sits down and gets to work."""
    ceo = _person("You (CEO)", -2.2, 1.0, seed=1, model="Casual_Male.gltf", role="CEO")
    eng = _person("Engineer", 0.0, -0.4, seed=3, model="Casual3_Male.gltf", role="Engineer")
    ceo_a = Actor("ceo", ceo, beats=[Face(0, "eng"), Hold(0, 11, "Idle"), Play(7.5, 3, "Victory")])
    eng_a = Actor("eng", eng, beats=[
        Hold(0, 1.6, "Idle"), Play(1.6, 2.0, "SitDown", loop=False), Hold(3.6, 8, "Idle")])
    shots = [
        Shot.dolly(0.0, 3.0, frm=(0.0, 1.8, 4.4), to=(0.0, 1.5, 2.4), look="eng",
                   fov=(42, 33), caption=("", "Hire your first engineer")),
        Shot.orbit(3.0, 3.4, center=(-1.0, 0.0, 0.3), radius=3.6, height=1.6,
                   deg=(20.0, 150.0), fov=37.0, caption=("ENGINEER", "Someone has to build it.")),
        Shot.dolly(6.4, 3.6, frm=(-2.2, 1.7, 3.2), to=(-2.2, 1.6, 2.0), look="ceo", fov=(40, 30)),
    ]
    return Scene([ceo_a, eng_a], shots, time_of_day="Afternoon", name="06_engineer"), [ceo, eng]


def _cut_website():
    """website · END — launch day; the small team celebrates the site going live."""
    pts = _ring(3, 1.7, start_deg=20.0)
    ceo = _person("You (CEO)", 0.0, 0.0, seed=1, model="Casual_Male.gltf", role="CEO")
    team = [ceo]
    actors = [Actor("ceo", ceo, beats=[Hold(0, 11, "Idle"), Play(5.5, 3.5, "Victory")])]
    for i, (x, z) in enumerate(pts):
        ch = _person(f"Team{i}", x, z, seed=10 + i)
        _face_center(ch)
        team.append(ch)
        actors.append(Actor(f"t{i}", ch, beats=[Hold(0, 5.4, "Idle"), Play(5.4, 4, "Victory")]))
    shots = [
        Shot.crane(0.0, 3.2, x=0.0, z=5.0, y=(4.4, 1.9), look=(0, 1, 0), fov=42.0,
                   caption=("", "Launch your website")),
        Shot.orbit(3.2, 3.6, center=(0, 0, 0), radius=4.4, height=1.9, deg=(0.0, 140.0),
                   fov=40.0),
        Shot.dolly(6.8, 3.4, frm=(0.0, 1.8, 4.6), to=(0.0, 1.6, 2.6), look="ceo",
                   fov=(42, 31), caption=("", "We're live.")),
    ]
    return Scene(actors, shots, time_of_day="Dusk", name="07_website"), team


def _cut_mvp():
    """mvp · END — the MVP ships; a tight huddle, crane down into the room."""
    pts = _ring(3, 1.5, start_deg=60.0)
    ceo = _person("You (CEO)", 0.0, 0.0, seed=1, model="Casual_Male.gltf", role="CEO")
    team = [ceo]
    actors = [Actor("ceo", ceo, beats=[Hold(0, 11, "Idle"), Play(6.0, 4, "Victory")])]
    for i, (x, z) in enumerate(pts):
        ch = _person(f"Team{i}", x, z, seed=20 + i)
        _face_center(ch)
        team.append(ch)
        actors.append(Actor(f"t{i}", ch, beats=[Hold(0, 6, "Idle"), Play(6.0, 4, "Victory")]))
    shots = [
        Shot.static(0.0, 2.6, pos=(5.6, 2.2, 5.6), look=(0, 1, 0), fov=40.0,
                    caption=("", "Ship the MVP")),
        Shot.crane(2.6, 3.2, x=0.0, z=4.4, y=(5.0, 1.8), look=(0, 1, 0), fov=38.0),
        Shot.orbit(5.8, 3.0, center=(0, 0, 0), radius=4.0, height=1.7, deg=(200.0, 320.0),
                   fov=37.0, caption=("", "v0.1 — shipped.")),
        Shot.dolly(8.8, 2.4, frm=(0.0, 1.7, 4.0), to=(0.0, 1.6, 2.4), look="ceo", fov=(38, 30)),
    ]
    return Scene(actors, shots, time_of_day="Evening", name="08_mvp"), team


def _cut_team10():
    """team10 · END — the company is a crowd now; a sweeping reveal of all ten."""
    ceo = _person("You (CEO)", 0.0, 0.0, seed=1, model="Casual_Male.gltf", role="CEO")
    team = [ceo]
    actors = [Actor("ceo", ceo, beats=[Hold(0, 13, "Idle"), Play(8.0, 4, "Victory")])]
    for i, (x, z) in enumerate(_ring(9, 3.0, start_deg=10.0)):
        ch = _person(f"Team{i}", x, z, seed=30 + i)
        _face_center(ch)
        team.append(ch)
        actors.append(Actor(f"t{i}", ch, beats=[Hold(0, 8, "Idle"), Play(8.0, 4, "Victory")]))
    shots = [
        Shot.crane(0.0, 4.0, x=0.0, z=6.4, y=(6.5, 2.4), look=(0, 1, 0), fov=46.0,
                   caption=("", "Grow the team to 10")),
        Shot.orbit(4.0, 4.4, center=(0, 0, 0), radius=6.0, height=2.4, deg=(0.0, 200.0),
                   fov=44.0, caption=("", "A company, not a project.")),
        Shot.dolly(8.4, 3.0, frm=(0.0, 2.0, 5.2), to=(0.0, 1.6, 2.8), look="ceo", fov=(44, 32)),
    ]
    return Scene(actors, shots, time_of_day="Noon", name="09_team10"), team


def _cut_series_a():
    """series_a · END — the win. Full team around the founder, the big finish."""
    ceo = _person("You (CEO)", 0.0, 0.0, seed=1, model="Casual_Male.gltf", role="CEO")
    team = [ceo]
    actors = [Actor("ceo", ceo, beats=[Hold(0, 14, "Idle"), Play(4.0, 8, "Victory")])]
    for i, (x, z) in enumerate(_ring(6, 2.6, start_deg=30.0)):
        ch = _person(f"Team{i}", x, z, seed=40 + i)
        _face_center(ch)
        team.append(ch)
        actors.append(Actor(f"t{i}", ch, beats=[Hold(0, 4.2, "Idle"), Play(4.2, 8, "Victory")]))
    shots = [
        Shot.dolly(0.0, 3.4, frm=(0.0, 1.7, 4.6), to=(0.0, 1.6, 2.6), look="ceo",
                   fov=(40, 30), caption=("", "Raise a Series A")),
        Shot.orbit(3.4, 4.6, center=(0, 0, 0), radius=5.2, height=2.2, deg=(0.0, 230.0),
                   fov=42.0, roll=(0.0, 3.0), caption=("", "The big leagues.")),
        Shot.crane(8.0, 3.2, x=0.0, z=5.0, y=(2.0, 6.2), look=(0, 1, 0), fov=44.0,
                   caption=("", "SERIES A")),
        Shot.static(11.2, 2.8, pos=(4.4, 2.0, 4.4), look=(0, 1.2, 0), fov=38.0,
                    caption=("", "You win.")),
    ]
    return Scene(actors, shots, time_of_day="Dusk", name="10_series_a"), team


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
@dataclass
class CutsceneDef:
    key: str                 # the tasks.py to-do this punctuates
    trigger: str             # "begin" | "middle" | "end" — when it plays
    title: str               # the to-do's title (for menus/logging)
    build: Callable          # () -> (Scene, list[Character])


CUTSCENES: dict[str, CutsceneDef] = {
    "name":      CutsceneDef("name",      "begin",  "Name your company",          _cut_name),
    "cofounder": CutsceneDef("cofounder", "middle", "Win over co-founder Robin",  _cut_cofounder),
    "seed":      CutsceneDef("seed",      "end",    "Raise your seed money",      _cut_seed),
    "office":    CutsceneDef("office",    "begin",  "Lease your first office",    _cut_office),
    "intern":    CutsceneDef("intern",    "middle", "Take on your first intern",  _cut_intern),
    "engineer":  CutsceneDef("engineer",  "end",    "Hire your first engineer",   _cut_engineer),
    "website":   CutsceneDef("website",   "end",    "Launch your website",        _cut_website),
    "mvp":       CutsceneDef("mvp",       "end",    "Ship the MVP",               _cut_mvp),
    "team10":    CutsceneDef("team10",    "end",    "Grow the team to 10",        _cut_team10),
    "series_a":  CutsceneDef("series_a",  "end",    "Raise a Series A",           _cut_series_a),
}

# render order (chapter order through the quest line)
ORDER = ["name", "cofounder", "seed", "office", "intern",
         "engineer", "website", "mvp", "team10", "series_a"]


def build(key: str) -> tuple[Scene, list]:
    """Return (Scene, chars) for a to-do key. Raises KeyError if unknown."""
    return CUTSCENES[key].build()

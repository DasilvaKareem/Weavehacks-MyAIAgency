"""A library of 10 cutscenes, one per to-do — now with narration + dialogue.

Each entry is a `CutsceneDef`: the to-do `key` it punctuates, a `trigger` for
*when* it plays (begin / middle / end), and a builder returning a ready-to-record
`cinematic.Scene` plus the Characters it animates.

Every cutscene carries a `script` track: `Narrate(...)` lines are voiceover
(centered, top) and `Say(speaker, ...)` lines are character dialogue (lower-third
subtitle). The two can overlap — a narrator under a character's line.

This is a *different* slate from the first pass (the founder-arc beats): this set
follows the build itself — pitch, customer, competition, brand, research, the
first all-hands, pricing, the campaign, the 1,000-user milestone, profitability.
All are authored in the office interior; pointing one at the city is just a
different `draw_world` callback at record time.

    from game import cutscenes
    scene, chars = cutscenes.build("meeting")
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable

import pyray as pr

from . import config, roster
from .entities import Character
from .cinematic import (Actor, Shot, Scene, Walk, Hold, Play, Face, Say, Narrate)

_MODELS = [
    "Casual_Male.gltf", "Casual2_Female.gltf", "Casual3_Male.gltf",
    "Casual_Female.gltf", "Casual2_Male.gltf", "Casual3_Female.gltf",
    "OldClassy_Male.gltf", "OldClassy_Female.gltf", "Casual_Bald.gltf",
    "BlueSoldier_Female.gltf",
]


def _person(name: str, x: float, z: float, *, seed: int,
            model: str | None = None, role: str = "") -> Character:
    m = model or _MODELS[seed % len(_MODELS)]
    ch = Character(name=name, role=role, x=x, z=z, color=pr.SKYBLUE, model=m)
    roster.apply_look(ch, roster.random_look(random.Random(seed * 7 + 13)))
    return ch


def _ring(n: int, radius: float, center=(0.0, 0.0), start_deg=0.0):
    out = []
    for i in range(n):
        a = math.radians(start_deg + 360.0 * i / n)
        out.append((center[0] + math.sin(a) * radius, center[1] + math.cos(a) * radius))
    return out


def _face(ch: Character, target=(0.0, 0.0)) -> None:
    ch.yaw = math.degrees(math.atan2(target[0] - ch.x, target[1] - ch.z))


# A CEO at a spot, facing a point — every scene has one.
def _ceo(x=0.0, z=0.0, face=(0.0, 1.0)) -> Character:
    c = _person("You (CEO)", x, z, seed=1, model="Casual_Male.gltf", role="CEO")
    _face(c, face)
    return c


# --------------------------------------------------------------------------- #
# the 10 cutscenes  (build the product → grow → profit)
# --------------------------------------------------------------------------- #
def _cut_pitch():
    """pitch · BEGIN — the founder alone, finding the one sentence."""
    ceo = _ceo(0.0, 0.0, face=(0.0, 6.0))
    a = Actor("ceo", ceo, beats=[Hold(0, 14, "Idle"), Play(10.5, 3.5, "Victory")])
    shots = [
        Shot.crane(0.0, 4.0, x=0.0, z=4.6, y=(1.3, 4.0), look="ceo", fov=40.0),
        Shot.dolly(4.0, 4.0, frm=(0.0, 1.6, 4.6), to=(0.0, 1.6, 2.4), look="ceo", fov=(42, 31)),
        Shot.static(8.0, 3.0, pos=(2.2, 1.7, 2.6), look="ceo", fov=34.0),
        Shot.dolly(11.0, 3.0, frm=(0.0, 1.7, 3.2), to=(0.0, 1.6, 2.1), look="ceo", fov=(36, 30)),
    ]
    script = [
        Narrate(0.4, 3.2, "Day one. No team, no money — just an idea in your head."),
        Say(4.2, 3.0, "You", "Okay... what do we actually do?"),
        Narrate(7.4, 3.0, "Say it in one sentence. If you can't, you don't have it yet."),
        Say(10.6, 3.2, "You", "\"We build AI agents that run your company.\""),
    ]
    return Scene([a], shots, script=script, time_of_day="Morning", name="01_pitch"), [ceo]


def _cut_customer():
    """customer · MIDDLE — you and Robin argue out who it's really for."""
    ceo = _ceo(-1.4, 0.6, face=(3.0, 0.6))
    robin = _person("Robin", 3.2, 0.6, seed=4, model="OldClassy_Female.gltf", role="Co-founder")
    _face(robin, (-1.4, 0.6))
    ceo_a = Actor("ceo", ceo, beats=[Hold(0, 14, "Idle"), Face(0.0, "robin")])
    robin_a = Actor("robin", robin, beats=[Hold(0, 14, "Idle"), Face(0.0, "ceo")])
    shots = [
        Shot.static(0.0, 3.0, pos=(0.8, 1.7, 4.8), look=(0.6, 1.2, 0.6), fov=40.0),
        Shot.orbit(3.0, 4.0, center=(0.6, 0.0, 0.6), radius=3.8, height=1.7,
                   deg=(210.0, 320.0), fov=37.0),
        Shot.dolly(7.0, 3.4, frm=(3.2, 1.7, 3.6), to=(3.2, 1.6, 2.2), look="robin", fov=(40, 31)),
        Shot.dolly(10.4, 3.4, frm=(-1.4, 1.7, 3.4), to=(-1.4, 1.6, 2.1), look="ceo", fov=(40, 30)),
    ]
    script = [
        Narrate(0.4, 3.0, "Every product is for someone. The trick is naming who."),
        Say(3.6, 3.0, "Robin", "So who's this actually for?"),
        Say(7.0, 3.2, "You", "Founders. People drowning in busywork."),
        Say(10.6, 3.2, "Robin", "Then build it for them. Only them."),
    ]
    return Scene([ceo_a, robin_a], shots, script=script, time_of_day="Afternoon",
                 name="02_customer"), [ceo, robin]


def _cut_competitors():
    """competitors · END — the analyst lays out the field."""
    ceo = _ceo(-2.0, 1.0, face=(0.0, -0.4))
    analyst = _person("Analyst", 0.2, -0.4, seed=7, model="OldClassy_Male.gltf", role="Analyst")
    _face(analyst, (-2.0, 1.0))
    ceo_a = Actor("ceo", ceo, beats=[Hold(0, 13, "Idle")])
    an_a = Actor("analyst", analyst, beats=[Hold(0, 13, "Idle")])
    shots = [
        Shot.dolly(0.0, 3.2, frm=(0.2, 1.8, 4.2), to=(0.2, 1.5, 2.4), look="analyst", fov=(42, 33)),
        Shot.orbit(3.2, 3.8, center=(-0.9, 0.0, 0.3), radius=3.6, height=1.6,
                   deg=(20.0, 140.0), fov=37.0),
        Shot.dolly(7.0, 3.0, frm=(-2.0, 1.7, 3.2), to=(-2.0, 1.6, 2.0), look="ceo", fov=(40, 31)),
        Shot.static(10.0, 3.0, pos=(2.0, 1.7, 2.6), look="ceo", fov=35.0),
    ]
    script = [
        Narrate(0.4, 3.0, "You're not the first. Know exactly who you're up against."),
        Say(3.6, 3.2, "Analyst", "Three big players. All slow, all bloated."),
        Say(7.2, 3.0, "You", "Then we win on speed."),
        Narrate(10.4, 2.6, "Find the gap they're too big to see."),
    ]
    return Scene([ceo_a, an_a], shots, script=script, time_of_day="Noon",
                 name="03_competitors"), [ceo, analyst]


def _cut_logo():
    """logo · MIDDLE — you brief the designer on the mark."""
    ceo = _ceo(-1.8, 0.8, face=(0.4, 0.8))
    des = _person("Designer", 0.6, 0.8, seed=5, model="Casual3_Female.gltf", role="Designer")
    _face(des, (-1.8, 0.8))
    ceo_a = Actor("ceo", ceo, beats=[Hold(0, 12, "Idle")])
    des_a = Actor("des", des, beats=[Hold(0, 7.5, "Idle"), Play(7.5, 3, "Victory"), Hold(10.5, 2, "Idle")])
    shots = [
        Shot.static(0.0, 2.8, pos=(0.0, 1.7, 4.6), look=(-0.6, 1.2, 0.8), fov=39.0),
        Shot.dolly(2.8, 3.2, frm=(-1.8, 1.7, 3.2), to=(-1.8, 1.6, 2.1), look="ceo", fov=(40, 31)),
        Shot.dolly(6.0, 3.2, frm=(0.6, 1.7, 3.4), to=(0.6, 1.6, 2.2), look="des", fov=(40, 31)),
        Shot.orbit(9.2, 3.0, center=(-0.6, 0.0, 0.8), radius=3.4, height=1.7, deg=(150, 30), fov=36.0),
    ]
    script = [
        Narrate(0.4, 2.4, "A brand starts with a single image."),
        Say(3.0, 3.0, "You", "Make it clean. One mark, unforgettable."),
        Say(6.2, 2.8, "Designer", "Give me a day."),
        Say(9.4, 2.6, "Designer", "...make it two."),
    ]
    return Scene([ceo_a, des_a], shots, script=script, time_of_day="Afternoon",
                 name="04_logo"), [ceo, des]


def _cut_research():
    """research · MIDDLE — the researcher comes back with the verdict."""
    ceo = _ceo(-1.6, 1.0, face=(0.6, -0.2))
    res = _person("Researcher", 4.0, -2.6, seed=8, model="Casual_Female.gltf", role="Researcher")
    ceo_a = Actor("ceo", ceo, beats=[Hold(0, 13, "Idle"), Face(3.6, "res")])
    res_a = Actor("res", res, beats=[
        Walk(0.4, 2.8, [(4.0, -2.6), (1.2, 0.2)], "Walk"), Face(3.4, "ceo"), Hold(3.4, 10, "Idle")])
    shots = [
        Shot.track(0.0, 3.2, target="res", offset=(-2.2, 1.6, -2.2), look="res", fov=38.0),
        Shot.orbit(3.2, 3.6, center=(-0.5, 0.0, 0.4), radius=3.6, height=1.7, deg=(150, 40), fov=37.0),
        Shot.dolly(6.8, 3.2, frm=(0.6, 1.7, 3.4), to=(0.6, 1.6, 2.2), look="res", fov=(40, 31)),
        Shot.dolly(10.0, 3.0, frm=(-1.6, 1.7, 3.2), to=(-1.6, 1.6, 2.0), look="ceo", fov=(40, 30)),
    ]
    script = [
        Say(0.6, 2.6, "Researcher", "I talked to forty users this week."),
        Say(3.6, 2.0, "You", "And?"),
        Say(6.0, 2.4, "Researcher", "They'd pay. Today."),
        Narrate(9.0, 3.4, "Validation — the rarest currency a startup ever holds."),
    ]
    return Scene([ceo_a, res_a], shots, script=script, time_of_day="Morning",
                 name="05_research"), [ceo, res]


def _cut_meeting():
    """meeting · END — the first all-hands, four voices in one room."""
    ceo = _ceo(0.0, 1.8, face=(0.0, -1.0))
    pts = [(-2.4, -0.6), (0.0, -1.4), (2.4, -0.6)]
    names = [("Engineer", "Casual3_Male.gltf"), ("Designer", "Casual3_Female.gltf"),
             ("Marketer", "Casual2_Male.gltf")]
    team = [ceo]
    actors = [Actor("ceo", ceo, beats=[Hold(0, 17, "Idle"), Play(13.5, 3.5, "Victory")])]
    for i, ((x, z), (nm, mdl)) in enumerate(zip(pts, names)):
        ch = _person(nm, x, z, seed=11 + i, model=mdl, role=nm)
        _face(ch, (0.0, 1.8))
        team.append(ch)
        actors.append(Actor(nm.lower(), ch, beats=[Hold(0, 13.5, "Idle"), Play(13.5, 4, "Victory")]))
    shots = [
        Shot.crane(0.0, 3.4, x=0.0, z=5.2, y=(4.6, 2.0), look=(0, 1, -0.2), fov=44.0),
        Shot.dolly(3.4, 3.0, frm=(0.0, 1.8, 4.4), to=(0.0, 1.6, 2.8), look="ceo", fov=(42, 33)),
        Shot.dolly(6.4, 2.6, frm=(-2.4, 1.6, 1.4), to=(-2.4, 1.6, 0.8), look="engineer", fov=36.0),
        Shot.dolly(9.0, 2.6, frm=(2.4, 1.6, 1.4), to=(2.4, 1.6, 0.8), look="marketer", fov=36.0),
        Shot.orbit(11.6, 3.0, center=(0, 0, 0.4), radius=4.4, height=2.0, deg=(0, 150), fov=42.0),
        Shot.dolly(14.6, 2.4, frm=(0.0, 1.8, 4.2), to=(0.0, 1.6, 2.6), look="ceo", fov=(40, 32)),
    ]
    script = [
        Narrate(0.4, 3.0, "The whole company — all of it — in one room."),
        Say(3.6, 2.8, "You", "We ship Friday. Everyone ready?"),
        Say(6.6, 2.2, "Engineer", "Backend's green."),
        Say(9.0, 2.2, "Designer", "UI's done."),
        Say(11.4, 2.4, "Marketer", "Launch post is locked and loaded."),
        Say(14.4, 2.4, "You", "Then let's go."),
    ]
    return Scene(actors, shots, script=script, time_of_day="Afternoon", name="06_meeting"), team


def _cut_pricing():
    """pricing · END — you and the analyst decide what you're worth."""
    ceo = _ceo(-1.6, 0.8, face=(0.4, 0.8))
    an = _person("Analyst", 0.8, 0.8, seed=7, model="OldClassy_Male.gltf", role="Analyst")
    _face(an, (-1.6, 0.8))
    ceo_a = Actor("ceo", ceo, beats=[Hold(0, 12, "Idle"), Play(8.6, 3, "Victory")])
    an_a = Actor("an", an, beats=[Hold(0, 12, "Idle")])
    shots = [
        Shot.dolly(0.0, 3.0, frm=(0.8, 1.7, 3.6), to=(0.8, 1.6, 2.2), look="an", fov=(40, 31)),
        Shot.orbit(3.0, 3.4, center=(-0.4, 0.0, 0.8), radius=3.4, height=1.7, deg=(20, 150), fov=37.0),
        Shot.dolly(6.4, 3.0, frm=(-1.6, 1.7, 3.2), to=(-1.6, 1.6, 2.0), look="ceo",
                   fov=(40, 30), roll=(0, -3)),
        Shot.static(9.4, 2.6, pos=(2.0, 1.7, 2.4), look="ceo", fov=34.0),
    ]
    script = [
        Narrate(0.4, 2.8, "Decide what you're worth — and say it out loud."),
        Say(3.2, 3.0, "Analyst", "Charge too little and they won't trust it."),
        Say(6.6, 2.6, "You", "Then we charge what it's worth."),
        Narrate(9.4, 2.4, "Pricing is a statement of confidence."),
    ]
    return Scene([ceo_a, an_a], shots, script=script, time_of_day="Noon",
                 name="07_pricing"), [ceo, an]


def _cut_campaign():
    """campaign · END — the marketer pitches the go-big plan."""
    ceo = _ceo(-1.8, 1.0, face=(0.6, 0.4))
    mk = _person("Marketer", 0.8, 0.4, seed=9, model="Casual2_Female.gltf", role="Marketer")
    _face(mk, (-1.8, 1.0))
    ceo_a = Actor("ceo", ceo, beats=[Hold(0, 12, "Idle"), Play(8.4, 3, "Victory")])
    mk_a = Actor("mk", mk, beats=[Hold(0, 6.4, "Idle"), Play(6.4, 2.4, "Victory"), Hold(8.8, 3, "Idle")])
    shots = [
        Shot.static(0.0, 2.8, pos=(0.2, 1.7, 4.4), look=(-0.6, 1.2, 0.7), fov=40.0),
        Shot.dolly(2.8, 3.4, frm=(0.8, 1.7, 3.4), to=(0.8, 1.6, 2.1), look="mk", fov=(40, 31)),
        Shot.orbit(6.2, 3.0, center=(-0.5, 0.0, 0.7), radius=3.4, height=1.7, deg=(150, 30), fov=36.0),
        Shot.dolly(9.2, 2.8, frm=(-1.8, 1.7, 3.2), to=(-1.8, 1.6, 2.0), look="ceo", fov=(40, 30)),
    ]
    script = [
        Narrate(0.4, 2.6, "You've built the fire. Now pour on the fuel."),
        Say(3.0, 3.0, "Marketer", "Thirty days. Every channel. All in."),
        Say(6.4, 2.4, "You", "Do it."),
        Narrate(9.4, 2.4, "Momentum is a choice you make on purpose."),
    ]
    return Scene([ceo_a, mk_a], shots, script=script, time_of_day="Dusk",
                 name="08_campaign"), [ceo, mk]


def _cut_users1k():
    """users1k · END — the 1,000-user milestone; the team feels it."""
    ceo = _ceo(0.0, 0.0, face=(0.0, 4.0))
    team = [ceo]
    actors = [Actor("ceo", ceo, beats=[Hold(0, 12, "Idle"), Play(6.5, 5, "Victory")])]
    for i, (x, z) in enumerate(_ring(4, 2.0, start_deg=20.0)):
        ch = _person(f"Team{i}", x, z, seed=21 + i)
        _face(ch)
        team.append(ch)
        actors.append(Actor(f"t{i}", ch, beats=[Hold(0, 6.4, "Idle"), Play(6.4, 5, "Victory")]))
    shots = [
        Shot.crane(0.0, 3.4, x=0.0, z=5.2, y=(4.6, 1.9), look=(0, 1, 0), fov=42.0),
        Shot.orbit(3.4, 3.6, center=(0, 0, 0), radius=4.4, height=1.9, deg=(0, 150), fov=40.0),
        Shot.dolly(7.0, 3.0, frm=(0.0, 1.8, 4.4), to=(0.0, 1.6, 2.6), look="ceo", fov=(42, 31)),
        Shot.static(10.0, 2.4, pos=(3.8, 1.9, 3.8), look=(0, 1.2, 0), fov=37.0),
    ]
    script = [
        Narrate(0.4, 3.2, "A thousand people you've never met chose you today."),
        Say(4.0, 2.6, "You", "One thousand. And we're just getting started."),
        Narrate(7.2, 2.8, "Traction you can finally feel."),
        Say(10.2, 2.2, "You", "Keep going."),
    ]
    return Scene(actors, shots, script=script, time_of_day="Morning", name="09_users1k"), team


def _cut_profitable():
    """profitable · END — the climb pays off; more in than out, at last."""
    ceo = _ceo(0.0, 0.0, face=(0.0, 4.0))
    team = [ceo]
    actors = [Actor("ceo", ceo, beats=[Hold(0, 15, "Idle"), Play(5.0, 9, "Victory")])]
    for i, (x, z) in enumerate(_ring(6, 2.6, start_deg=30.0)):
        ch = _person(f"Team{i}", x, z, seed=41 + i)
        _face(ch)
        team.append(ch)
        actors.append(Actor(f"t{i}", ch, beats=[Hold(0, 5.2, "Idle"), Play(5.2, 9, "Victory")]))
    shots = [
        Shot.dolly(0.0, 3.4, frm=(0.0, 1.7, 4.6), to=(0.0, 1.6, 2.6), look="ceo", fov=(40, 30)),
        Shot.orbit(3.4, 4.4, center=(0, 0, 0), radius=5.2, height=2.2, deg=(0, 220),
                   fov=42.0, roll=(0, 3)),
        Shot.crane(7.8, 3.4, x=0.0, z=5.0, y=(2.0, 6.0), look=(0, 1, 0), fov=44.0),
        Shot.static(11.2, 2.8, pos=(4.4, 2.0, 4.4), look=(0, 1.2, 0), fov=37.0),
    ]
    script = [
        Narrate(0.4, 3.2, "More coming in than going out. For the first time — ever."),
        Say(4.0, 2.4, "You", "We're profitable."),
        Narrate(7.0, 3.0, "Not a project anymore. Not a bet. A company."),
        Narrate(11.2, 2.6, "You built this."),
    ]
    return Scene(actors, shots, script=script, time_of_day="Dusk", name="10_profitable"), team


# --------------------------------------------------------------------------- #
# the demo reel  (how it works: the problem → Redis → Weave)
# --------------------------------------------------------------------------- #
# A standalone 3-beat reel for the 60-second "how we built it" pitch. It tells
# one story: a world full of autonomous agents is one bad loop from collapse —
# Redis gives that world a body (it knows where everything is), and W&B Weave
# gives it a conscience (it watches every call and optimizes the agents itself).
def _cut_chaos():
    """problem · BEGIN — a world full of agents, and nobody knows where anyone is."""
    ceo = _ceo(0.0, 0.0, face=(0.0, 4.0))
    # Five agents on crossing paths: motion without coordination — the chaos.
    wanderers = [
        ("eng", "Engineer",   "Casual3_Male.gltf",   31, [(-5.0, -3.0), (-1.0, 2.0), (3.0, -1.0)]),
        ("des", "Designer",   "Casual3_Female.gltf", 32, [(4.8, 3.2), (0.8, -2.0), (-3.4, 1.6)]),
        ("ana", "Analyst",    "OldClassy_Male.gltf",  33, [(4.6, -3.4), (0.4, 0.6), (-4.2, 3.0)]),
        ("mar", "Marketer",   "Casual2_Male.gltf",    34, [(-4.6, 3.4), (-0.4, -0.6), (4.2, -2.8)]),
        ("res", "Researcher", "Casual_Female.gltf",   35, [(3.2, 4.0), (-1.8, 1.0), (-5.0, -2.6)]),
    ]
    team = [ceo]
    actors = [Actor("ceo", ceo, beats=[
        Hold(0, 14, "Idle"), Face(2.0, "eng"), Face(5.5, "mar"), Face(8.5, "des")])]
    for key, nm, mdl, seed, path in wanderers:
        ch = _person(nm, path[0][0], path[0][1], seed=seed, model=mdl, role=nm)
        team.append(ch)
        actors.append(Actor(key, ch, beats=[Walk(0.0, 11.0, path, "Walk"), Hold(11.0, 3.5, "Idle")]))
    shots = [
        Shot.crane(0.0, 3.4, x=0.0, z=6.6, y=(6.2, 2.4), look=(0, 1, 0), fov=46.0),
        Shot.orbit(3.4, 4.0, center=(0, 0, 0), radius=6.6, height=2.6, deg=(0, 140), fov=44.0),
        Shot.dolly(7.4, 3.2, frm=(0.0, 1.7, 4.2), to=(0.0, 1.6, 2.4), look="ceo", fov=(42, 32)),
        Shot.static(10.6, 3.6, pos=(4.8, 2.0, 4.8), look=(0, 1.2, 0), fov=40.0),
    ]
    script = [
        Narrate(0.4, 3.4, "A living world — dozens of AI agents talking, deciding, working."),
        Narrate(4.0, 3.6, "Now the hard part: how do you stop it collapsing into chaos?"),
        Say(8.0, 2.8, "You", "Where is everyone? Who's even doing what?"),
        Narrate(11.2, 3.0, "A sandbox of agents is one bad loop from bankruptcy."),
    ]
    return Scene(actors, shots, script=script, time_of_day="Morning", name="11_chaos"), team


def _cut_redis():
    """redis · MIDDLE — Redis gives the world a body: it knows where everything is."""
    ceo = _ceo(-4.0, 2.6, face=(1.0, 0.0))
    des = _person("Designer", 3.0, -1.0, seed=42, model="Casual3_Female.gltf", role="Designer")
    eng = _person("Engineer", -3.0, 3.0, seed=43, model="Casual3_Male.gltf", role="Engineer")
    # Two stationary teammates = entities indexed on the live city map.
    bg1 = _person("Analyst", 4.6, 3.0, seed=44, model="OldClassy_Male.gltf", role="Analyst")
    bg2 = _person("Marketer", -4.2, -3.0, seed=45, model="Casual2_Male.gltf", role="Marketer")
    _face(bg1, (0, 0)); _face(bg2, (0, 0))
    actors = [
        Actor("ceo", ceo, beats=[Hold(0, 20, "Idle"), Face(0.0, "eng"), Face(7.0, "des")]),
        Actor("eng", eng, beats=[
            Walk(0.4, 3.0, [(-3.0, 3.0), (0.0, 1.0), (1.4, -0.6)], "Walk"),
            Face(3.4, "des"), Hold(3.4, 17, "Idle")]),
        Actor("des", des, beats=[
            Hold(0, 13.6, "Idle"), Face(3.0, "eng"),
            Play(13.6, 3.0, "Victory"), Hold(16.6, 4, "Idle")]),
        Actor("bg1", bg1, beats=[Hold(0, 20, "Idle")]),
        Actor("bg2", bg2, beats=[Hold(0, 20, "Idle")]),
    ]
    shots = [
        Shot.track(0.0, 3.4, target="eng", offset=(-2.6, 1.6, -2.4), look="eng", fov=38.0),
        Shot.dolly(3.4, 3.4, frm=(1.4, 1.7, 2.8), to=(1.4, 1.6, 1.4), look="des", fov=(40, 32)),
        Shot.orbit(6.8, 3.4, center=(0.7, 0.0, 0.2), radius=3.8, height=1.7, deg=(200, 330), fov=38.0),
        Shot.dolly(10.2, 3.4, frm=(0.7, 1.7, 3.6), to=(0.7, 1.6, 2.2), look="eng", fov=(40, 31)),
        Shot.dolly(13.6, 3.0, frm=(1.4, 1.7, 2.8), to=(1.4, 1.6, 1.6), look="des", fov=(40, 30)),
        Shot.crane(16.6, 3.4, x=0.0, z=5.2, y=(2.0, 5.2), look=(0.5, 1.2, 0.0), fov=42.0),
    ]
    script = [
        Narrate(0.4, 3.2, "First — Redis isn't our database. It's the agent's sense of space."),
        Narrate(3.8, 3.2, "Every second the world streams every position into Redis."),
        Say(7.2, 2.8, "Engineer", "Who's nearest the bug? ...Designer. On my way."),
        Narrate(10.2, 3.2, "Geospatial queries — 'who's near me?' — answered in microseconds."),
        Say(13.6, 2.8, "Designer", "Solved this last sprint. Here — take it."),
        Narrate(16.6, 3.4, "And vector memory: every agent recalls what the whole team has learned."),
    ]
    return Scene(actors, shots, script=script, time_of_day="Afternoon", name="12_redis"), \
        [ceo, eng, des, bg1, bg2]


def _cut_weave():
    """weave · END — Weave gives the world a conscience: it optimizes the agents itself."""
    ceo = _ceo(-2.0, 1.6, face=(0.6, 0.6))
    obs = _person("Observability", 0.6, 0.6, seed=51, model="OldClassy_Female.gltf", role="DevOps")
    weak = _person("Engineer", 3.2, -1.0, seed=52, model="Casual2_Male.gltf", role="Engineer")
    _face(obs, (-2.0, 1.6)); _face(weak, (0.6, 0.6))
    team = [ceo, obs, weak]
    actors = [
        Actor("ceo", ceo, beats=[Hold(0, 23.4, "Idle"), Face(0.0, "obs"), Face(13.0, "weak")]),
        Actor("obs", obs, beats=[
            Hold(0, 13.6, "Idle"), Face(7.0, "weak"),
            Play(13.6, 3.0, "Victory"), Hold(16.6, 7, "Idle")]),
        Actor("weak", weak, beats=[Hold(0, 16.6, "Idle"), Play(16.6, 6.8, "Victory")]),
    ]
    for i, (x, z) in enumerate(((-3.2, -2.6), (2.2, -3.0))):
        ch = _person(("Marketer", "Designer")[i], x, z, seed=53 + i,
                     model=("Casual_Female.gltf", "Casual_Bald.gltf")[i],
                     role=("Marketer", "Designer")[i])
        _face(ch, (0, 0))
        team.append(ch)
        actors.append(Actor(f"t{i}", ch, beats=[Hold(0, 17.6, "Idle"), Play(17.6, 6, "Victory")]))
    shots = [
        Shot.dolly(0.0, 3.6, frm=(0.6, 1.7, 4.0), to=(0.6, 1.5, 2.4), look="obs", fov=(42, 33)),
        Shot.orbit(3.6, 3.8, center=(0.3, 0.0, 0.6), radius=3.8, height=1.7, deg=(20, 150), fov=38.0),
        Shot.dolly(7.4, 3.4, frm=(0.6, 1.7, 3.4), to=(0.6, 1.6, 2.1), look="obs", fov=(40, 31)),
        Shot.dolly(10.8, 2.8, frm=(-2.0, 1.7, 3.2), to=(-2.0, 1.6, 2.0), look="ceo", fov=(40, 30)),
        Shot.dolly(13.6, 3.4, frm=(3.2, 1.7, 2.6), to=(3.2, 1.6, 1.2), look="weak", fov=(40, 31)),
        Shot.crane(17.0, 3.4, x=0.0, z=5.2, y=(2.0, 5.6), look=(0.4, 1.2, 0.0), fov=44.0),
        Shot.orbit(20.4, 3.0, center=(0, 0, 0), radius=4.8, height=2.2, deg=(0, 150),
                   fov=42.0, roll=(0, 3)),
    ]
    script = [
        Narrate(0.4, 3.4, "Knowing where everyone is means nothing if the agents keep getting worse."),
        Narrate(3.8, 3.6, "So Weave watches every decision — every call, every cost, every mistake."),
        Say(7.6, 3.0, "Observability", "This one's burning cash for weak results."),
        Say(10.8, 2.6, "You", "Then rewrite how it works."),
        Narrate(13.6, 3.8, "An agent reads the company's own telemetry and fixes the weakest worker — itself."),
        Narrate(17.6, 3.0, "In our demo, costs fell twenty-one percent. No human in the loop."),
        Say(20.8, 2.6, "You", "Redis gives it a body. Weave gives it a conscience."),
    ]
    return Scene(actors, shots, script=script, time_of_day="Dusk", name="13_weave"), team


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
    "pitch":       CutsceneDef("pitch",       "begin",  "Write your one-line pitch",  _cut_pitch),
    "customer":    CutsceneDef("customer",    "middle", "Name your target customer",  _cut_customer),
    "competitors": CutsceneDef("competitors", "end",    "Size up the competition",    _cut_competitors),
    "logo":        CutsceneDef("logo",        "middle", "Design a logo",              _cut_logo),
    "research":    CutsceneDef("research",     "middle", "Run market research",        _cut_research),
    "meeting":     CutsceneDef("meeting",     "end",    "Hold an all-hands",          _cut_meeting),
    "pricing":     CutsceneDef("pricing",     "end",    "Set your pricing",           _cut_pricing),
    "campaign":    CutsceneDef("campaign",    "end",    "Run a marketing campaign",   _cut_campaign),
    "users1k":     CutsceneDef("users1k",     "end",    "Reach 1,000 users",          _cut_users1k),
    "profitable":  CutsceneDef("profitable",  "end",    "Turn profitable",            _cut_profitable),
    # the standalone "how it works" demo reel (the 60-second pitch)
    "chaos":       CutsceneDef("chaos",       "begin",  "The problem: agent chaos",   _cut_chaos),
    "redis":       CutsceneDef("redis",       "middle", "Redis: the world's body",    _cut_redis),
    "weave":       CutsceneDef("weave",       "end",    "Weave: the conscience",      _cut_weave),
}

# render order (chapter order through the quest line)
ORDER = ["pitch", "customer", "competitors", "logo", "research",
         "meeting", "pricing", "campaign", "users1k", "profitable"]

# the demo reel, in pitch order (problem → Redis → Weave)
DEMO_ORDER = ["chaos", "redis", "weave"]


def build(key: str) -> tuple[Scene, list]:
    """Return (Scene, chars) for a to-do key. Raises KeyError if unknown."""
    return CUTSCENES[key].build()

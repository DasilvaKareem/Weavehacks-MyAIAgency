"""The single registry of NAMED story/quest characters.

Before this file, every named NPC (Robin the co-founder, Bob, Mae, Walter,
Biscuit, Río, the intern — and Sam in the prologue) was hand-built inline:
~10 lines apiece of `Character(...)` + `roster.apply_look(...)` +
`voice.pick_voice(...)`, all repeated in `Game.__init__`. There was no one
place to see "who are the named characters?" — adding or restyling one meant
hunting through main.py.

This module is that one place. Each character's *identity* (name, role,
department, model, fixed appearance, facing, whether it speaks) lives in the
`NAMED` table as data; `build_named()` turns the table into ready-to-use
`Character` objects plus their picked TTS voices. The bespoke *quest logic and
state* (e.g. `_bob_done`, `_pet_stage`) deliberately stays in main.py — this
registry owns who they ARE, not what their quests DO.

Note Robin's name is sourced from `COFOUNDER_NAME` (the company-graph identity),
not a literal, so the two never drift; see the entry below.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pyray as pr

from . import roster, voice
from .entities import Character
from .coordinator_link import COFOUNDER_NAME


@dataclass(frozen=True)
class NPCDef:
    """The identity of one named character — everything needed to spawn it.

    `look` is the fixed appearance dict passed to `roster.apply_look` (so the
    model's raw ~black "Skin" material never shows); `None` means leave the
    model's own materials alone (e.g. Biscuit the pug keeps its fur). `voice`
    flags characters that speak aloud, so `build_named` picks them a TTS voice.
    """
    key: str
    name: str
    role: str
    dept: str
    model: str
    color: tuple[int, int, int, int]
    look: dict | None = None
    yaw: float = 0.0                # matches Character's own default
    voice: bool = False


# The named cast. Keys are stable internal ids; main.py aliases a few to their
# historical attribute names (self.robin, self.lady=Mae, self.civilian=Walter,
# self.pet=Biscuit, self.busker=Río) so the rest of the game needs no changes.
NAMED: dict[str, NPCDef] = {
    "robin": NPCDef(
        key="robin", name=COFOUNDER_NAME, role="Co-founder", dept="Founder",
        model="Suit_Male.gltf", color=(90, 210, 230, 255), yaw=180.0,
        look={"skin_idx": 2, "hair_idx": 2, "eye_idx": 1, "suit_idx": 2},
        voice=True),
    "intern": NPCDef(
        key="intern", name="Eager Intern", role="Intern", dept="Operations",
        model="Casual_Male.gltf", color=(150, 210, 120, 255), yaw=0.0,
        look={"skin_idx": 4, "hair_idx": 0, "eye_idx": 1}),
    "bob": NPCDef(
        key="bob", name="Bob", role="Old Friend", dept="Friend",
        model="Casual2_Male.gltf", color=(225, 170, 120, 255), yaw=180.0,
        look={"skin_idx": 2, "hair_idx": 2, "hair_style": 1, "eye_idx": 2},
        voice=True),
    "mae": NPCDef(
        key="mae", name="Mae", role="Small-Biz Desk", dept="Civic",
        model="Suit_Female.gltf", color=(210, 150, 190, 255), yaw=180.0,
        look={"skin_idx": 3, "hair_idx": 3, "hair_style": 2, "eye_idx": 1},
        voice=True),
    "walter": NPCDef(
        key="walter", name="Walter", role="Resident", dept="Civic",
        model="Casual3_Male.gltf", color=(200, 200, 120, 255), yaw=180.0,
        look={"skin_idx": 1, "hair_idx": 5, "hair_style": 0, "eye_idx": 0},
        voice=True),
    "biscuit": NPCDef(
        key="biscuit", name="Biscuit", role="Pug", dept="",
        model="Pug.gltf", color=(210, 180, 140, 255)),   # animal: keep its fur
    "rio": NPCDef(
        key="rio", name="Río", role="Busker", dept="Civic",
        model="Casual2_Female.gltf", color=(120, 180, 210, 255), yaw=180.0,
        look={"skin_idx": 5, "hair_idx": 1, "hair_style": 3, "eye_idx": 2},
        voice=True),
    "banker": NPCDef(
        key="banker", name="Vivian", role="Bank Manager", dept="First City Bank",
        model="Suit_Female.gltf", color=(120, 200, 150, 255), yaw=180.0,
        look={"skin_idx": 1, "hair_idx": 4, "hair_style": 2, "eye_idx": 1},
        voice=True),
    # Sam, the prologue relocation guide. Built by game/prologue.py (which runs
    # before Game exists), but defined here so the named cast is all in one place.
    "sam": NPCDef(
        key="sam", name="Sam", role="Guide", dept="",
        model="Casual_Male.gltf", color=(255, 203, 0, 255),   # pr.GOLD
        look={"skin_idx": 3, "hair_idx": 4, "eye_idx": 1}),
}


def make(key: str) -> Character:
    """Spawn one named character from the registry (appearance applied)."""
    d = NAMED[key]
    ch = Character(name=d.name, role=d.role, x=0.0, z=0.0,
                   color=pr.Color(*d.color), dept=d.dept, model=d.model, yaw=d.yaw)
    if d.look is not None:
        roster.apply_look(ch, d.look)
    return ch


def build_named(keys: list[str] | None = None
                ) -> tuple[dict[str, Character], dict[str, str | None]]:
    """Build the named cast: returns (characters_by_key, voices_by_key).

    `keys` selects a subset (default: everyone except Sam, who the prologue
    builds itself). Voices are only picked for `voice=True` entries.
    """
    if keys is None:
        keys = [k for k in NAMED if k != "sam"]
    chars: dict[str, Character] = {}
    voices: dict[str, str | None] = {}
    for k in keys:
        d = NAMED[k]
        chars[k] = make(k)
        if d.voice:
            voices[k] = voice.pick_voice(d.name)
    return chars, voices

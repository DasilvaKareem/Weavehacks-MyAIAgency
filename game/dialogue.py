"""Data-driven NPC dialogue for story beats.

The story *progression* lives in game/tasks.py (the ordered quest line). This
module holds what characters *say* at each beat, so authoring a scene is a JSON
edit (assets/dialogue.json) rather than a code change — the old hardcoded
single-line `QUEST_LINES` dict didn't scale past a handful of stops.

A beat is keyed by a tasks.py task key and holds an ordered list of `Line`s
shown one at a time before the player fills in that task's `ask` (the final line
shows alongside the input box). A JSON line is either a plain string (spoken by
the beat's default `who`) or {"who","text"} to switch speaker mid-scene. `who`
may be empty — the caller falls back to the building/NPC name.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

PATH = os.path.join(os.path.dirname(__file__), os.pardir, "assets", "dialogue.json")

# Shown when a quest stop has no authored beat (keeps the game running).
FALLBACK = "Let's get this sorted — fill it in for me."


@dataclass(frozen=True)
class Line:
    who: str        # speaker; "" => caller uses the NPC/building name
    text: str


@dataclass(frozen=True)
class Beat:
    lines: tuple[Line, ...]


def _beat(raw: dict) -> Beat:
    default_who = raw.get("who", "") or ""
    out: list[Line] = []
    for ln in raw.get("lines", []):
        if isinstance(ln, str):
            out.append(Line(default_who, ln))
        else:
            out.append(Line(ln.get("who", default_who) or default_who, ln["text"]))
    return Beat(tuple(out))


def load(path: str = PATH) -> dict[str, Beat]:
    """Map task key -> Beat. Returns {} if the file is missing/unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f).get("beats", {})
    except (OSError, ValueError):
        return {}
    return {k: _beat(v) for k, v in data.items() if isinstance(v, dict)}


def lines_for(beats: dict[str, Beat], key: str) -> tuple[Line, ...]:
    """The lines for a beat, or a single generic fallback line."""
    beat = beats.get(key)
    if beat and beat.lines:
        return beat.lines
    return (Line("", FALLBACK),)

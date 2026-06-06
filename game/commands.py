"""Parse CEO chat messages into bot movement commands.

The CEO commands bots in plain language through the normal 1:1 chat — "go to the
meeting room", "follow me", "go talk to Sarah", "back to work", "gather everyone".
This module recognizes those intents and turns them into a structured Intent the
game applies to the bot's brain (USER priority, preempting its routine). Anything
that isn't a movement command returns None and flows on to the LLM as usual.

Kept deliberately lightweight (keyword/regex matching, no model call) so issuing
a command is instant and free.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import zones

# Spoken phrasings -> canonical zone name. First matching synonym wins; longer,
# more specific synonyms are checked before short ones.
_ZONE_SYNONYMS: dict[str, list[str]] = {
    "meeting":    ["meeting room", "conference room", "conference", "boardroom",
                   "meeting", "the table"],
    "coffee":     ["coffee machine", "break room", "water cooler", "kitchen",
                   "pantry", "coffee", "cooler"],
    "whiteboard": ["whiteboard", "the board", "brainstorm"],
    "lounge":     ["lounge", "couch", "sofa", "sitting area"],
    "door":       ["front door", "entrance", "lobby", "exit", "door"],
}

_FOLLOW = ("follow me", "come with me", "come here", "come along", "follow",
           "with me", "let's go")
_BACK = ("back to work", "back to your desk", "return to your desk", "get back to work",
         "resume work", "go back to work", "back to it")
_GATHER = ("everyone", "all hands", "the team", "everybody", "all of you", "standup",
           "stand up", "team meeting")
# Phrases unambiguous enough to gather the whole team on their own (no verb needed).
_GATHER_STRONG = ("all hands", "team meeting", "everyone to", "everybody to",
                  "everyone in", "everyone here", "gather every", "gather the team",
                  "call everyone", "get everyone", "bring everyone", "all of you")

_TALK_RE = re.compile(
    r"(?:talk to|speak (?:to|with)|chat with|go (?:see|talk to)|check in with|"
    r"sync with|catch up with)\s+([a-z][\w'’-]*)", re.I)
_GOTO_RE = re.compile(
    r"(?:go (?:to|over to)|head (?:to|over to)|walk (?:to|over to)|move (?:to|over to)|"
    r"get (?:to|over to))\s+(?:the\s+)?(.+)", re.I)


@dataclass
class Intent:
    kind: str                 # follow | goto | talk_to | back_to_work | gather
    target: object = None     # zone name (str), a peer Character, or None
    ack: str = ""             # what the bot says back
    all_bots: bool = False    # gather: applies to the whole team


def _match_zone(text: str) -> str | None:
    low = text.lower()
    for zone, syns in _ZONE_SYNONYMS.items():
        if zone not in zones.all_names():
            continue
        for s in syns:
            if s in low:
                return zone
    return None


def _first_name(name: str) -> str:
    return (name or "").split()[0] if name else ""


def _match_peer(token: str, agent, agents):
    """Resolve a spoken name to another agent Character (by first name)."""
    tok = token.strip().lower().strip(".,!?'\"")
    for a in agents:
        if a is agent:
            continue
        if _first_name(a.name).lower() == tok or a.name.lower() == tok:
            return a
    return None


def parse(text: str, agent, agents) -> Intent | None:
    """Return an Intent if `text` is a movement command for `agent`, else None.

    `agent` is the Character being chatted with; `agents` is the full roster (to
    resolve "talk to <name>").
    """
    low = " ".join(text.lower().split())
    if not low:
        return None

    # back to work — check before follow/goto so "go back to work" isn't a goto.
    if any(p in low for p in _BACK) or low in ("back", "desk", "sit down"):
        return Intent("back_to_work", ack="Heading back to my desk.")

    # gather everyone — team-wide, before single-bot goto.
    if (any(p in low for p in _GATHER_STRONG)
            or (("gather" in low or "bring" in low or "call" in low or "get" in low)
                and any(p in low for p in _GATHER))
            or low.strip("!. ") in _GATHER):
        return Intent("gather", target="meeting", all_bots=True,
                      ack="On my way to the meeting.")

    # follow the CEO
    if any(p in low for p in _FOLLOW):
        return Intent("follow", ack="Right behind you.")

    # talk to a named teammate
    m = _TALK_RE.search(low)
    if m:
        peer = _match_peer(m.group(1), agent, agents)
        if peer is not None:
            return Intent("talk_to", target=peer,
                          ack=f"Going to find {_first_name(peer.name)}.")

    # go to a place
    m = _GOTO_RE.search(low)
    if m:
        rest = m.group(1)
        if "desk" in rest or "work" in rest:
            return Intent("back_to_work", ack="Heading back to my desk.")
        peer = None
        for tok in rest.split():
            peer = _match_peer(tok, agent, agents)
            if peer is not None:
                return Intent("talk_to", target=peer,
                              ack=f"Going to find {_first_name(peer.name)}.")
        zone = _match_zone(rest)
        if zone is not None:
            return Intent("goto", target=zone, ack=f"Heading to the {zone}.")

    # a bare place name ("the kitchen", "meeting room")
    zone = _match_zone(low)
    if zone is not None and ("go" in low or "to the" in low or low.startswith("the ")):
        return Intent("goto", target=zone, ack=f"Heading to the {zone}.")

    return None

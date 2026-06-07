"""Authors a movement *policy* for a bot — the deliberative tier of the office AI.

The game's behaviour layer (game/behavior.py) executes a BotPolicy every frame
for free; this module is what fills that policy in, occasionally and off the
render thread (via CompanyLink's worker pool). It runs on hire and on notable
events, never per frame.

Split of labour, chosen to keep token cost near zero:
  * Personality knobs (sociability / restlessness / focus / explore) are DERIVED
    from the agent's deterministic OCEAN persona — no model call. So even with no
    API key a bot still moves in character (explore = how far it'll roam, e.g. up
    to other floors/wings through the elevator and doorways).
  * The LLM only authors the flavourful parts: which zones this character would
    haunt (route), a handful of in-character banter lines, and a one-word mood.

Everything degrades gracefully: any failure (no key, bad JSON, timeout) still
returns the free persona-derived knobs, just with an empty route/banter so the
caller keeps the bot's defaults.
"""
from __future__ import annotations

import json
import logging

from .persona import generate as make_persona

log = logging.getLogger("company.planner")

_MAX_BANTER = 7
_MAX_ROUTE = 5


def derive_knobs(persona) -> dict:
    """Map OCEAN traits -> the three movement knobs (all 0..1). Pure + free."""
    t = persona.traits
    extra = t["extraversion"]
    consc = t["conscientiousness"]
    openn = t["openness"]

    def clamp(v: float) -> float:
        return round(max(0.1, min(0.95, v)), 2)

    return {
        "sociability": clamp(extra / 100.0),
        # restless = curious (openness) and not too buttoned-down (low consc.)
        "restlessness": clamp((openn * 0.5 + (100 - consc) * 0.5) / 100.0),
        "focus": clamp(consc / 100.0),
        # explore = how far they'll wander — curious (openness) AND outgoing
        # (extraversion) people are the ones who roam to other floors/wings.
        "explore": clamp((openn * 0.6 + extra * 0.4) / 100.0),
    }


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: -3]
        # drop a leading 'json' language tag if present
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    return t.strip()


def _parse(text: str, zone_names: list[str]) -> dict:
    """Pull route/banter/mood out of the model's reply, validated against zones."""
    try:
        data = json.loads(_strip_fences(text))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    allowed = set(zone_names)
    route = [z for z in data.get("route", []) if z in allowed][:_MAX_ROUTE]
    banter = [str(b).strip() for b in data.get("banter", []) if str(b).strip()][:_MAX_BANTER]
    mood = str(data.get("mood", "") or "").strip().lower()[:24] or "neutral"
    out: dict = {"mood": mood}
    if route:
        out["route"] = route
    if banter:
        out["banter"] = banter
    return out


def _prompt(name: str, role: str, persona, zone_names: list[str], context: str) -> str:
    zones_line = ", ".join(zone_names)
    ctx = f"\nCurrent company context: {context}" if context else ""
    return (
        f"You are setting the daily *office behaviour* for {name}, a {role} at an "
        f"AI company, for a 3D office game. {persona.blurb()}.{ctx}\n\n"
        f"The office has these places a person can walk to: {zones_line}.\n\n"
        "Return ONLY a JSON object (no prose, no code fence) with:\n"
        '  "route":  an ordered list of 2-4 of those place names this person '
        "would naturally wander between when not at their desk (fit their role "
        "and personality),\n"
        '  "banter": 5-7 short first-person one-liners (max ~8 words each) this '
        "person might say in passing around the office, in their voice,\n"
        '  "mood":   one lowercase word for their current vibe.\n'
        'Example: {"route":["coffee","whiteboard"],"banter":["Need anything from me?"],'
        '"mood":"focused"}'
    )


def plan_policy(agent_id: str, role: str, name: str,
                zone_names: list[str], context: str = "") -> dict:
    """Return a policy dict: {sociability, restlessness, focus, [route], [banter], mood}.

    Blocking (makes one model call); intended to run on a worker thread. Always
    returns at least the free persona-derived knobs.
    """
    persona = make_persona(agent_id, role)
    out = derive_knobs(persona)
    out["mood"] = "neutral"
    try:
        from .llm import get_llm   # lazy: avoids importing the SDK when keyless

        llm = get_llm()
        reply = llm.invoke(_prompt(name, role, persona, zone_names, context))
        text = getattr(reply, "content", reply)
        if isinstance(text, list):  # some providers return content blocks
            text = " ".join(b.get("text", "") if isinstance(b, dict) else str(b)
                            for b in text)
        out.update(_parse(text, zone_names))
    except Exception as exc:  # no key, parse/timeout error -> knobs-only
        log.info("policy planning fell back to knobs only for %s: %s", name, exc)
    return out

"""The Film Director's tool: author a cinematic film, starring the real company.

`direct_film(title, brief, seconds)` has the LLM write a compact film SPEC — a cast
(by role) and a timed script (narrator + character lines) — and saves it to the
company drive at /films/<slug>.json. The game's cinematic recorder
(`cinematic_demo.py --film /films/<slug>.json`) then renders it to an MP4 with the
real CEO + hired agents and each speaker in their own Gemini voice
(game/films.py + game/cast.py).

This module is pure backend (an LLM call + a drive write) — no raylib — so an agent
can run it on a worker thread. Rendering needs a GL context, so it stays a separate
step the game/CLI performs.
"""
from __future__ import annotations

import json
import re

_TIMES = {"Morning", "Afternoon", "Evening", "Night"}
_MAX_TOTAL = 28.0          # keep films short (and cheap to voice/render)
_MIN_DUR, _MAX_DUR = 1.5, 6.0


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or "film")[:48]


def _castable_roles(store) -> list[str]:
    """Roles the film can cast: the CEO plus every hired, non-fired role."""
    roles = ["CEO"]
    for a in store.list_agents():
        if getattr(a, "status", "") != "fired" and a.role not in roles:
            roles.append(a.role)
    return roles


def _author_spec(llm, title: str, brief: str, seconds: float, roles: list[str]) -> dict:
    from .agents import _text

    system = (
        "You are a film director writing a SHORT cinematic for an AI company. "
        "Output ONLY a JSON object (no prose, no markdown fences) with this shape:\n"
        '{\n'
        '  "title": str,\n'
        '  "time_of_day": "Morning"|"Afternoon"|"Evening"|"Night",\n'
        '  "cast": [{"role": <one of the castable roles>, "label": <short name used as the speaker>}],\n'
        '  "script": [{"t": seconds_float, "dur": seconds_float, "kind": "narrate"|"say", "speaker": <a cast label, or "" for narrate>, "text": str}]\n'
        "}\n"
        "Rules: ALWAYS include the CEO in the cast. Only cast roles from the castable "
        "list. Lines play at time t for dur seconds; order them and don't overlap two "
        "'say' lines. Open with a narrator line. Keep it tight and punchy — a few "
        f"lines totaling about {int(seconds)}s, max {int(_MAX_TOTAL)}s. Make 'say' "
        "lines sound like the real person speaking in first person."
    )
    user = (f"Castable roles: {', '.join(roles)}\n"
            f"Title: {title}\nBrief: {brief}\nTarget length: ~{int(seconds)}s\n"
            "Write the film spec JSON now.")
    raw = _text(llm.invoke([("system", system), ("human", user)])).strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)        # tolerate stray fences/prose
    if not m:
        raise ValueError("director returned no JSON")
    return json.loads(m.group(0))


def _normalize(spec: dict, title: str, roles: list[str]) -> dict:
    """Clamp + sanitize an authored spec into something build_film can always render."""
    tod = spec.get("time_of_day")
    tod = tod if tod in _TIMES else "Afternoon"

    cast, seen_labels = [], set()
    for c in spec.get("cast") or []:
        role = str(c.get("role", "")).strip()
        if role not in roles:                        # only real, castable roles
            continue
        label = str(c.get("label") or role).strip()[:24] or role
        if label in seen_labels:
            continue
        seen_labels.add(label)
        cast.append({"role": role, "label": label})
    if not any(c["role"] == "CEO" for c in cast):    # CEO is always in it
        cast.insert(0, {"role": "CEO", "label": "CEO"})
    labels = {c["label"] for c in cast}

    lines = []
    for ln in spec.get("script") or []:
        text = str(ln.get("text", "")).strip()
        if not text:
            continue
        kind = "narrate" if ln.get("kind") == "narrate" else "say"
        try:
            t = max(0.0, float(ln.get("t", 0.0)))
            dur = float(ln.get("dur", 3.0))
        except (TypeError, ValueError):
            continue
        dur = max(_MIN_DUR, min(_MAX_DUR, dur))
        speaker = "" if kind == "narrate" else str(ln.get("speaker", "")).strip()
        if kind == "say" and speaker not in labels:  # unknown speaker → make it narration
            kind, speaker = "narrate", ""
        lines.append({"t": round(t, 2), "dur": round(dur, 2),
                      "kind": kind, "speaker": speaker, "text": text[:240]})
    lines.sort(key=lambda l: l["t"])
    # Bound total length.
    lines = [l for l in lines if l["t"] < _MAX_TOTAL]
    for l in lines:
        l["dur"] = min(l["dur"], _MAX_TOTAL - l["t"])

    return {"title": (spec.get("title") or title or "Untitled Film").strip()[:80],
            "slug": _slug(spec.get("title") or title),
            "time_of_day": tod, "cast": cast, "script": lines}


def load_film_tools(agent_id: str | None = None, agent_name: str = "") -> list:
    """The direct_film tool for the Film Director."""
    from langchain_core.tools import tool

    @tool
    def direct_film(title: str, brief: str, seconds: int = 20) -> str:
        """Direct a SHORT cinematic film of the company and save it to the drive.
        `title` names it, `brief` describes what it should show/say, `seconds` is the
        rough target length (15-25 is best). Writes a cast (the real CEO + hired
        teammates) and a timed script. Does NOT render the video here — returns the
        drive path + the command to render it. Use this whenever the CEO wants a
        teaser, recap, or launch film."""
        from .llm import get_llm
        from .store import AgentStore

        store = AgentStore()
        roles = _castable_roles(store)
        try:
            raw = _author_spec(get_llm(), title, brief, float(seconds or 20), roles)
            spec = _normalize(raw, title, roles)
        except Exception as exc:
            return f"[couldn't write the film: {exc}]"
        if not spec["script"]:
            return "[the film came back empty — try a more specific brief]"

        path = f"/films/{spec['slug']}.json"
        store.fs_write(path, json.dumps(spec, indent=2),
                       author_id=agent_id, author_name=agent_name or "Film Director")
        cast = ", ".join(c["role"] for c in spec["cast"])
        return (f"Wrote the film '{spec['title']}' to {path} — cast: {cast}; "
                f"{len(spec['script'])} lines. Render it with:  "
                f"python cinematic_demo.py --film {path}")

    return [direct_film]

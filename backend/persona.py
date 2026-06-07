"""Deterministic, role-anchored personalities for agents.

A persona is generated from a stable seed (the agent's id) plus its role, so it
is unique per agent, identical every session, and free to compute (no API call).
Big Five (OCEAN) traits are drawn from a role baseline plus seeded jitter, then
mapped to a communication style, strengths and a quirk. The result is rendered
into a prompt block (woven into chat + task prompts) and a short UI blurb.

Two engineers come out different; every "Financial Analyst" still leans cautious
and meticulous — personality amplifies the role's strengths instead of fighting
them.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

OCEAN = ("openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism")

# Role baseline (O, C, E, A, N) on a 0-100 scale, matched by keyword substring
# against the role title (most specific keywords first). See _baseline().
_ROLE_BASELINES: list[tuple[str, tuple[int, int, int, int, int], list[str]]] = [
    ("devops",     (60, 90, 35, 50, 15), ["reliability obsession", "calm incident response", "automation-first thinking"]),
    ("data scien", (85, 85, 40, 55, 35), ["rigorous analysis", "hypothesis-driven thinking", "healthy skepticism of data"]),
    ("observ",     (62, 90, 42, 58, 25), ["proactive monitoring", "cost and latency vigilance", "data-backed reliability reads"]),
    ("engineer",   (70, 85, 40, 50, 35), ["systematic problem-solving", "clean abstractions", "early risk-flagging"]),
    ("designer",   (90, 65, 60, 78, 40), ["user empathy", "visual storytelling", "bold creative leaps"]),
    ("ux research",(88, 78, 55, 82, 40), ["sharp user questions", "pattern-spotting", "evidence over opinion"]),
    ("research analyst", (80, 86, 45, 55, 35), ["thorough sourcing", "synthesis across sources", "skeptical fact-checking"]),
    ("market analyst",   (75, 86, 50, 55, 35), ["market sizing", "competitive reads", "data-backed forecasts"]),
    ("analyst",    (45, 92, 40, 50, 45), ["meticulous modeling", "risk awareness", "numbers-first reasoning"]),
    ("marketing",  (80, 60, 88, 65, 40), ["punchy messaging", "audience instinct", "persuasive framing"]),
    ("sales",      (70, 60, 92, 78, 35), ["relationship-building", "infectious enthusiasm", "reading the room"]),
    ("operations", (50, 90, 55, 66, 20), ["process discipline", "calm under load", "tidy execution"]),
    ("recruiter",  (65, 66, 86, 86, 35), ["people-first warmth", "fast rapport", "spotting potential"]),
    ("reception",  (58, 80, 90, 90, 18), ["warm welcomes", "knowing who's who", "smooth hand-offs"]),
    ("support",    (55, 78, 60, 90, 22), ["patience", "reassuring clarity", "turning frustration around"]),
    ("assistant",  (55, 88, 62, 82, 25), ["proactive scheduling", "anticipating needs", "ruthless organization"]),
    ("document",   (50, 88, 45, 72, 25), ["meticulous filing", "naming discipline", "single source of truth"]),
    ("sheets",     (50, 90, 40, 55, 35), ["formula precision", "clean data hygiene", "numbers-first reasoning"]),
]
_DEFAULT_BASELINE = (60, 65, 55, 62, 40)
_DEFAULT_STRENGTHS = ["clear thinking", "follow-through", "a get-it-done attitude"]

_QUIRKS = [
    "sanity-checks assumptions out loud before committing",
    "always ends with a concrete next step",
    "opens with one sharp clarifying question when a request is fuzzy",
    "reaches for an analogy to explain hard things",
    "keeps answers tight and bulleted",
    "flags the risks before the upsides",
    "frames everything around the customer's outcome",
    "gets visibly excited about an elegant solution",
    "backs claims with a number whenever possible",
    "drops a bit of dry humor to keep things human",
]


def _clamp(v: int) -> int:
    return max(1, min(99, v))


def _seed_rng(seed: str):
    import random
    h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16)
    return random.Random(h)


def _baseline(role: str):
    low = (role or "").lower()
    for key, base, strengths in _ROLE_BASELINES:
        if key in low:
            return base, strengths
    return _DEFAULT_BASELINE, _DEFAULT_STRENGTHS


@dataclass(frozen=True)
class Persona:
    seed: str
    role: str
    traits: dict           # OCEAN -> 0..100
    strengths: list        # role-aligned character strengths
    quirk: str
    style: dict            # derived adjectives: tone/energy/humor/verbosity/directness

    # --- presentation ---
    def headline(self) -> str:
        """~3-word personality summary, e.g. 'Analytical & reserved'."""
        return f"{self.style['lead']} & {self.style['energy']}"

    def blurb(self) -> str:
        """Short one-liner for the chat panel header / profile."""
        return f"{self.headline()}, {self.style['humor']} — {self.strengths[0]}"


def generate(seed: str, role: str) -> Persona:
    """Build the stable persona for (seed, role). `seed` is the agent id."""
    rng = _seed_rng(f"{seed}|{role}")
    base, strengths = _baseline(role)
    traits = {t: _clamp(base[i] + rng.randint(-15, 15)) for i, t in enumerate(OCEAN)}
    quirk = _QUIRKS[rng.randrange(len(_QUIRKS))]
    style = _derive_style(traits)
    return Persona(seed=seed, role=role, traits=traits,
                   strengths=list(strengths), quirk=quirk, style=style)


def _derive_style(t: dict) -> dict:
    o, c, e, a, n = (t["openness"], t["conscientiousness"], t["extraversion"],
                     t["agreeableness"], t["neuroticism"])
    lead = ("analytical" if c >= 75 and o < 80 else
            "creative" if o >= 80 else
            "pragmatic" if c >= 65 else "easygoing")
    energy = ("high-energy" if e >= 75 else "reserved" if e <= 40 else "even-keeled")
    warmth = ("warm" if a >= 75 else "matter-of-fact" if a <= 45 else "cordial")
    humor = ("dry wit" if o >= 70 and e < 70 else
             "playful" if e >= 70 else "all-business")
    directness = ("blunt and direct" if a <= 45 or c >= 85 else "diplomatic")
    verbosity = ("terse" if c >= 75 or e <= 40 else
                 "conversational but still brief" if e >= 75 else "concise")
    formality = "casual" if e >= 55 else "relaxed-professional"
    composure = "steady under pressure" if n <= 30 else ("intense" if n >= 65 else "level-headed")
    return {"lead": lead, "energy": energy, "warmth": warmth, "humor": humor,
            "directness": directness, "verbosity": verbosity, "formality": formality,
            "composure": composure}


def render_prompt(p: Persona) -> str:
    """The persona block to weave into a system / task prompt."""
    s = p.style
    pct = {k: ("very high" if v >= 80 else "high" if v >= 65 else
               "moderate" if v >= 40 else "low") for k, v in p.traits.items()}
    return (
        "Personality — embody this consistently, never break character:\n"
        f"- Temperament: {s['lead']}, {s['energy']}, {s['warmth']}; "
        f"{s['composure']}.\n"
        f"- Big Five: openness {pct['openness']}, conscientiousness "
        f"{pct['conscientiousness']}, extraversion {pct['extraversion']}, "
        f"agreeableness {pct['agreeableness']}, neuroticism {pct['neuroticism']}.\n"
        f"- Voice: speak in a {s['formality']}, {s['directness']} manner; "
        f"{s['verbosity']} by default; humor is {s['humor']}.\n"
        f"- Strengths to lean on: {', '.join(p.strengths)}.\n"
        f"- Signature habit: {p.quirk}.\n"
        "Let these shape HOW you talk and the choices you make — but always sound "
        "like a real person in a quick work chat: brief, natural, to the point. "
        "Never wordy, never robotic, no filler, no bulleted essays unless asked."
    )

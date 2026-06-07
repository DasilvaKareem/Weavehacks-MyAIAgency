"""Names, roles and skin-tone helpers for generating hire candidates."""
from __future__ import annotations

import random
import pyray as pr

from . import config

# Roles: (title, department, accent color used on labels / fallback boxes).
ROLES = [
    ("Software Engineer", "Engineering", pr.SKYBLUE),
    ("Data Scientist", "Engineering", pr.DARKBLUE),
    ("Product Designer", "Design", pr.MAGENTA),      # Gemini image generation
    ("Graphic Designer", "Design", pr.PINK),         # Gemini image generation
    ("Animator", "Design", pr.PURPLE),               # Gemini Veo video generation
    ("Film Director", "Design", pr.Color(90, 120, 160, 255)),  # directs cast+voiced cutscene films
    ("UX Researcher", "Design", pr.PINK),
    ("Marketing Lead", "Growth", pr.ORANGE),
    ("Blogger", "Growth", pr.BROWN),                 # Daytona: builds + publishes a real site
    ("Sales Rep", "Growth", pr.GOLD),
    ("Financial Analyst", "Finance", pr.GREEN),
    ("Operations Manager", "Operations", pr.VIOLET),
    ("Receptionist", "Operations", pr.Color(235, 180, 120, 255)),  # front desk: greets you in the lobby
    ("Recruiter", "People", pr.LIME),
    ("Human Resources Manager", "People", pr.RED),   # manages/evaluates/fires agents
    ("People Analytics Lead", "People", pr.DARKPURPLE),  # W&B Weave: grades the team on QUALITY (evals/leaderboard/feedback)
    ("Support Specialist", "Operations", pr.SKYBLUE),
    ("DevOps Engineer", "Engineering", pr.RED),      # Opsera-powered: see backend ROLE_PROFILES
    ("Observability Engineer", "Engineering", pr.DARKPURPLE),  # W&B Weave: reads the company's LLM traces
    ("Research Analyst", "Research", pr.PURPLE),     # Apify: web search
    ("Market Analyst", "Research", pr.MAROON),       # Apify: e-commerce / market data
    ("Executive Assistant", "Operations", pr.YELLOW),  # Composio: Gmail + Calendar
    ("Document Manager", "Operations", pr.BEIGE),      # Composio: Drive + Docs
    ("Sheets Analyst", "Finance", pr.DARKGREEN),       # Composio: Google Sheets
    # Sales Rep, Marketing Lead, Recruiter (above) are now Apify-powered too —
    # routed to their actors by keyword in backend ROLE_PROFILES.
]

# Hire rate per role — what it costs to bring this title on board. Roughly scales
# with seniority / specialization; surfaced as the "pricing rate" beside each role
# in the phone Hire app's department grid. Anything not listed falls back to
# DEFAULT_RATE.
DEFAULT_RATE = 2_500
ROLE_RATES = {
    "Software Engineer": 3_000,
    "Data Scientist": 3_500,
    "DevOps Engineer": 4_000,
    "Observability Engineer": 3_500,
    "Product Designer": 3_000,
    "Graphic Designer": 2_500,
    "Animator": 3_000,
    "Film Director": 3_500,
    "UX Researcher": 2_500,
    "Marketing Lead": 3_000,
    "Blogger": 2_000,
    "Sales Rep": 2_000,
    "Financial Analyst": 3_000,
    "Sheets Analyst": 2_500,
    "Operations Manager": 3_500,
    "Receptionist": 1_500,
    "Support Specialist": 1_500,
    "Executive Assistant": 2_000,
    "Document Manager": 2_000,
    "Research Analyst": 3_500,
    "Market Analyst": 3_500,
    "Recruiter": 2_500,
    "Human Resources Manager": 3_500,
    "People Analytics Lead": 3_500,
}


def role_rate(title: str) -> int:
    """Hire cost for a role title (see ROLE_RATES)."""
    return ROLE_RATES.get(title, DEFAULT_RATE)


def departments() -> list[tuple[str, list[tuple[str, "pr.Color", int]]]]:
    """Roles grouped by department in first-seen order. Each role is
    (title, accent_color, rate). Drives the Hire app's role grid."""
    order: list[str] = []
    groups: dict[str, list] = {}
    for title, dept, color in ROLES:
        if dept not in groups:
            groups[dept] = []
            order.append(dept)
        groups[dept].append((title, color, role_rate(title)))
    return [(d, groups[d]) for d in order]


# A diverse pool of first names.
FIRST_NAMES = [
    "Amara", "Liang", "Sofia", "Omar", "Priya", "Diego", "Mei", "Kwame",
    "Yuki", "Aisha", "Mateo", "Nina", "Tariq", "Elena", "Jamal", "Ravi",
    "Chloe", "Hassan", "Lucia", "Sven", "Zara", "Andre", "Fatima", "Noah",
    "Ingrid", "Kenji", "Rosa", "Idris", "Hana", "Marco",
]

LAST_NAMES = [
    "Okafor", "Chen", "Reyes", "Haddad", "Patel", "Santos", "Wang", "Mensah",
    "Tanaka", "Khan", "Rossi", "Novak", "Silva", "Kim", "Ali", "Nguyen",
    "Dubois", "Larsson", "Costa", "Ibrahim", "Schmidt", "Moreau", "Olsen",
]


def random_name(used_names: set[str] | None = None) -> str:
    """A random first+last name, avoiding `used_names` when possible."""
    used = used_names or set()
    name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    for _ in range(40):
        if name not in used:
            break
        name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    return name


def generate(index: int, used_names: set[str]) -> dict:
    """Build a fresh hire candidate. Role cycles for an even department spread;
    name is random and de-duplicated; skin tone defaults to a random tone."""
    title, dept, color = ROLES[index % len(ROLES)]
    name = random_name(used_names)
    return {
        "name": name,
        "role": title,
        "dept": dept,
        "color": color,
        "tone_idx": random.randrange(len(config.SKIN_TONES)),
    }


def random_look(rng=None) -> dict:
    """A full random appearance for an auto-generated hire candidate: random
    skin / hair color / hairstyle / eye color indices (the same keys _spawn_agent
    and _commit_hire expect). Pass `rng` (e.g. a seeded random.Random) for a
    reproducible look, else the module-global random is used."""
    r = rng or random
    return {
        "skin_idx": r.randrange(len(config.SKIN_TONES)),
        "hair_idx": r.randrange(len(config.HAIR_COLORS)),
        "hair_style": r.randrange(len(config.HAIRSTYLES)),
        "eye_idx": r.randrange(len(config.EYE_COLORS)),
    }


def apply_look(ch, look: dict) -> None:
    """Tint a Character to a human appearance from a look dict (skin_idx / hair_idx /
    eye_idx / suit_idx / hair_style; any missing key defaults to 0).

    Without this, a spawned Character keeps the model's raw base materials — whose
    "Skin" is near-black (~rgb 3,3,3) — so the NPC renders solid black. Every
    human NPC that isn't a hired agent or the CEO must be run through this."""
    ch.skin_tone = tone_color(look.get("skin_idx", 0))
    ch.hair_tone = palette_color(config.HAIR_COLORS, look.get("hair_idx", 0))
    ch.eye_tone = palette_color(config.EYE_COLORS, look.get("eye_idx", 0))
    ch.outfit_tone = palette_color(config.SUIT_COLORS, look.get("suit_idx", 0))
    ch.hair_style = look.get("hair_style", 0)


def palette_color(palette: list, idx: int) -> pr.Color:
    """Pick a pyray Color from a [(name, (r, g, b)), ...] palette, wrapping idx."""
    _, (r, g, b) = palette[idx % len(palette)]
    return pr.Color(r, g, b, 255)


def tone_color(tone_idx: int) -> pr.Color:
    return palette_color(config.SKIN_TONES, tone_idx)

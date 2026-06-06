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
    ("UX Researcher", "Design", pr.PINK),
    ("Marketing Lead", "Growth", pr.ORANGE),
    ("Blogger", "Growth", pr.BROWN),                 # Daytona: builds + publishes a real site
    ("Sales Rep", "Growth", pr.GOLD),
    ("Financial Analyst", "Finance", pr.GREEN),
    ("Operations Manager", "Operations", pr.VIOLET),
    ("Recruiter", "People", pr.LIME),
    ("Human Resources Manager", "People", pr.RED),   # manages/evaluates/fires agents
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
    "UX Researcher": 2_500,
    "Marketing Lead": 3_000,
    "Blogger": 2_000,
    "Sales Rep": 2_000,
    "Financial Analyst": 3_000,
    "Sheets Analyst": 2_500,
    "Operations Manager": 3_500,
    "Support Specialist": 1_500,
    "Executive Assistant": 2_000,
    "Document Manager": 2_000,
    "Research Analyst": 3_500,
    "Market Analyst": 3_500,
    "Recruiter": 2_500,
    "Human Resources Manager": 3_500,
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


def random_look() -> dict:
    """A full random appearance for an auto-generated hire candidate: random
    skin / hair color / hairstyle / eye color indices (the same keys _spawn_agent
    and _commit_hire expect)."""
    return {
        "skin_idx": random.randrange(len(config.SKIN_TONES)),
        "hair_idx": random.randrange(len(config.HAIR_COLORS)),
        "hair_style": random.randrange(len(config.HAIRSTYLES)),
        "eye_idx": random.randrange(len(config.EYE_COLORS)),
    }


def palette_color(palette: list, idx: int) -> pr.Color:
    """Pick a pyray Color from a [(name, (r, g, b)), ...] palette, wrapping idx."""
    _, (r, g, b) = palette[idx % len(palette)]
    return pr.Color(r, g, b, 255)


def tone_color(tone_idx: int) -> pr.Color:
    return palette_color(config.SKIN_TONES, tone_idx)

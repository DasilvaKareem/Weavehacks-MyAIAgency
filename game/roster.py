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
    ("Research Analyst", "Research", pr.PURPLE),     # Apify: web search
    ("Market Analyst", "Research", pr.MAROON),       # Apify: e-commerce / market data
    ("Executive Assistant", "Operations", pr.YELLOW),  # Composio: Gmail + Calendar
    ("Document Manager", "Operations", pr.BEIGE),      # Composio: Drive + Docs
    ("Sheets Analyst", "Finance", pr.DARKGREEN),       # Composio: Google Sheets
    # Sales Rep, Marketing Lead, Recruiter (above) are now Apify-powered too —
    # routed to their actors by keyword in backend ROLE_PROFILES.
]

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


def palette_color(palette: list, idx: int) -> pr.Color:
    """Pick a pyray Color from a [(name, (r, g, b)), ...] palette, wrapping idx."""
    _, (r, g, b) = palette[idx % len(palette)]
    return pr.Color(r, g, b, 255)


def tone_color(tone_idx: int) -> pr.Color:
    return palette_color(config.SKIN_TONES, tone_idx)

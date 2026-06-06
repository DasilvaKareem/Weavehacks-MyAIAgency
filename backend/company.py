"""The company's identity — the CEO's decisions, shared into every agent's brain.

The player makes real decisions while playing (name, pitch, target customer,
business model, pricing, brand, competitors). Those are persisted by the game into
the SQLite `settings` table under `company_profile`. This module is the ONE place
the backend reads them and renders the prompt block woven into every agent's system
prompt — 1:1 chat, meetings, autonomous jobs, the movement planner — so the whole
company reasons from the same shared understanding of what it's building and for whom.

Pure + store-only (no API calls): cheap to call on every prompt build.
"""
from __future__ import annotations

import json

# Settings keys. `company_profile` is the canonical business profile written by the
# game; `ceo_profile` is the older blob that already holds name/pitch (kept as a
# fallback so existing saves still surface those two before the player revisits them).
COMPANY_KEY = "company_profile"
CEO_KEY = "ceo_profile"

# (profile key, human label) in the order they read best in the prompt.
_FIELDS: list[tuple[str, str]] = [
    ("name", "Company name"),
    ("industry", "Industry"),
    ("pitch", "What we do"),
    ("value_prop", "Value proposition"),
    ("customer", "Target customer"),
    ("channels", "Channels"),
    ("relationships", "Customer relationships"),
    ("business_model", "Business model"),
    ("pricing", "Pricing"),
    ("key_resources", "Key resources"),
    ("key_activities", "Key activities"),
    ("partnerships", "Key partners"),
    ("cost_structure", "Cost structure"),
    ("brand", "Brand"),
    ("domain", "Website / domain"),
    ("logo", "Logo"),
    ("competitors", "Competitors"),
]


def _read(store, key: str) -> dict:
    try:
        raw = store.get_setting(key)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def load_profile(store) -> dict:
    """The company's decided facts as a flat dict (only set fields are present).

    Reads `company_profile`, then backfills name/pitch from the legacy `ceo_profile`
    so a save made before this feature still tells agents the company name."""
    prof = {k: str(v).strip() for k, v in _read(store, COMPANY_KEY).items()
            if isinstance(v, (str, int, float)) and str(v).strip()}
    ceo = _read(store, CEO_KEY)
    if not prof.get("name") and ceo.get("company_name"):
        prof["name"] = str(ceo["company_name"]).strip()
    if not prof.get("pitch") and ceo.get("pitch"):
        prof["pitch"] = str(ceo["pitch"]).strip()
    return prof


def render_context(profile: dict) -> str:
    """The company-context block for a system prompt, or "" if nothing's decided yet."""
    lines = [f"- {label}: {profile[key]}" for key, label in _FIELDS if profile.get(key)]
    if not lines:
        return ""
    name = profile.get("name") or "the company"
    return (
        f"This is the company you work for. Everything you say and do must stay "
        f"consistent with it, and serve {name}'s goals and customer:\n"
        "--- YOUR COMPANY ---\n" + "\n".join(lines) + "\n--- END ---"
    )


def context_for(store) -> str:
    """Convenience: load + render the company context block straight from the store."""
    return render_context(load_profile(store))


def one_liner(store) -> str:
    """A compact single line for low-stakes prompts (e.g. the movement planner)."""
    p = load_profile(store)
    if not p:
        return ""
    bits = [p["name"]] if p.get("name") else []
    if p.get("pitch"):
        bits.append(p["pitch"])
    if p.get("customer"):
        bits.append(f"for {p['customer']}")
    return " — ".join(bits)

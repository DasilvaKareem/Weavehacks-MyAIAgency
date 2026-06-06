"""Investor meetings — raise a funding round once you've done your homework.

A VC writes a big lump-sum check, but only if you've actually captured the things
that matter: your company profile (name, pitch, customer, business model, and —
for the bigger rounds — the full business model canvas). Each round needs more
captured than the last, and pays more. The captured facts live in the company
profile (game/company_link.save_company), filled in by to-dos and quest stops.

Pure data + evaluation; the meeting UI is game/investor_panel.py.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Round:
    key: str
    name: str
    amount: int
    needs: tuple        # company-profile keys that must be filled in to qualify
    line: str           # what the investor says


# Human labels for the company-profile keys the investor checks off.
LABELS = {
    "name": "Company name",
    "pitch": "One-line pitch",
    "customer": "Target customer",
    "competitors": "Competitor analysis",
    "business_model": "Business model",
    "pricing": "Pricing",
    "value_prop": "Value proposition",
    "channels": "Channels",
    "relationships": "Customer relationships",
    "key_resources": "Key resources",
    "key_activities": "Key activities",
    "partnerships": "Key partners",
    "cost_structure": "Cost structure",
}

_CORE = ("name", "pitch", "customer", "business_model")
_SEED = _CORE + ("competitors", "value_prop", "pricing")
_CANVAS = _SEED + ("channels", "relationships", "key_resources",
                   "key_activities", "partnerships", "cost_structure")

ROUNDS: list[Round] = [
    Round("preseed", "Pre-seed", 10_000, _CORE,
          "Pre-seed is a bet on the founder and the idea. Walk me through what you've got."),
    Round("seed", "Seed", 50_000, _SEED,
          "Seed money is about a real plan - your market, your edge, how you make money."),
    Round("seriesa", "Series A", 250_000, _CANVAS,
          "Series A is the whole picture. I want the entire business model canvas, airtight."),
]


def _has(company: dict, key: str) -> bool:
    v = company.get(key)
    return bool(v and str(v).strip())


def missing(company: dict, needs) -> list[str]:
    return [k for k in needs if not _has(company, k)]


def qualifies(company: dict, rnd: Round) -> bool:
    return not missing(company, rnd.needs)


def next_round(raised) -> Round | None:
    """The next round you haven't raised yet, or None once they're all closed."""
    for r in ROUNDS:
        if r.key not in raised:
            return r
    return None

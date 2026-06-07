"""Procedural identities for the city's backdrop buildings.

Every block the city-builder fills with a scenery model used to be a dead facade —
you could see it but never walk up to it. This module gives each one a stable,
generated identity (a named business with a kind and a greeting) so the whole
city is alive: walk up + E and the shopfront talks back. A deterministic subset
hand out a small one-time cash reward, so exploring every street actually pays.

Everything is derived deterministically from the building's model + grid position
(no RNG state), so a given block is the SAME business every run — names, perks
and all — and nothing here needs persistence beyond the "already visited" flag
that main.py keeps for the cash payouts.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

# --- the business taxonomy --------------------------------------------------
# Each scenery model leans toward a kind of tenant: a building with a shop-sign
# reads as retail, a tiny cottage as a residence, a tower as offices, and so on.
# The kind decides the name shape, the greeting voice, and whether it pays out.

_FOOD = "food"
_RETAIL = "retail"
_SERVICE = "service"
_OFFICE = "office"
_HOME = "home"
_NIGHTLIFE = "nightlife"

# model substring -> kind (first match wins; order matters). Anything unmatched
# falls back to a position-hashed pick from _DEFAULT_KINDS so small/plain models
# still get variety instead of all becoming the same thing.
_MODEL_KIND = [
    ("Sign", _RETAIL),            # models with a blank shop-sign read as storefronts
    ("Sidehouse", _HOME),
    ("Stairs", _HOME),
    ("RoundRoof", _FOOD),
    ("GableRoof", _FOOD),
    ("Columns", _SERVICE),        # columned facade = bank/clinic/civic vibe
    ("Balcony", _OFFICE),
    ("6Story", _OFFICE),
    ("4Story", _OFFICE),
    ("3Story", _OFFICE),
]
_DEFAULT_KINDS = [_RETAIL, _FOOD, _SERVICE, _HOME, _OFFICE, _NIGHTLIFE]

# Proper-name parts — combined into a "{proper} {noun}" sign for most tenants.
_PROPERS = [
    "Marlowe", "Hartwell", "Crest", "Vance", "Goldhill", "Marisol", "Quill",
    "Ardent", "Briar", "Kestrel", "Drummond", "Linden", "Vesper", "Calder",
    "Orsini", "Pemberton", "Ravel", "Sutton", "Thorne", "Wexler", "Yardley",
    "Ashby", "Beckett", "Cortez", "Delacroix", "Esposito", "Faraday", "Greer",
    "Holloway", "Ishikawa", "Janssen", "Kovac", "Larkin", "Moreno", "Nakamura",
    "Okafor", "Petrov", "Rosales", "Saito", "Tremblay", "Underhill", "Volkov",
]
_AMPERSAND = [
    "Bean & Toast", "Salt & Cedar", "Pine & Penny", "Ink & Iron", "Sage & Stone",
    "Copper & Clove", "Fox & Field", "Loom & Lantern", "Reed & Rye", "Maple & Main",
]

# Per-kind nouns + greeting lines. Greetings are picked by position hash so the
# same block always says the same thing.
_KINDS = {
    _FOOD: {
        "nouns": ["Diner", "Cafe", "Bakery", "Noodle Bar", "Taqueria", "Bistro",
                  "Coffee House", "Deli", "Tea Room", "Grill"],
        "tag": "Open · grab a bite",
        "greet": [
            "Smells incredible in here. “First coffee's on the house, boss — go build something big.”",
            "The cook waves you in. “Founders eat free on a Monday. Don't tell the others.”",
            "“Rough launch? Sit. Eat. The city looks better on a full stomach.”",
            "Warm bread, loud kitchen. “We cater board meetings, you know. Keep us in mind.”",
        ],
    },
    _RETAIL: {
        "nouns": ["Goods", "Supply Co.", "Outfitters", "Mercantile", "Hardware",
                  "Stationers", "Trading Co.", "Provisions", "Emporium", "Wares"],
        "tag": "Open · browsing welcome",
        "greet": [
            "A bell jingles. “Just browsing? Take your time — every empire starts with a list.”",
            "“We stock a little of everything. If we don't have it, the place next door might.”",
            "The owner nods. “Buy local, hire local, that's how a town grows. Good luck out there.”",
            "“Not hiring today, but leave a card — you never know.”",
        ],
    },
    _SERVICE: {
        "nouns": ["Clinic", "Law Offices", "Insurance", "Notary", "Credit Union",
                  "Dental", "Tax & Books", "Realty", "Agency", "Repair Shop"],
        "tag": "Open · by appointment",
        "greet": [
            "A receptionist looks up. “No appointment? For a founder we'll make time. What do you need?”",
            "“We keep the small businesses on this street running. Glad to add you to the list.”",
            "Quiet waiting room. “Paperwork's the boring half of building something. We handle it.”",
            "“Come back when you're incorporated — we'll sort the rest.”",
        ],
    },
    _OFFICE: {
        "nouns": ["Holdings", "Partners", "Labs", "Studio", "Group", "Ventures",
                  "Systems", "Collective", "Works", "& Co."],
        "tag": "Tenants · lobby open",
        "greet": [
            "The lobby hums with other startups. “Another founder? Welcome to the block.”",
            "A security guard nods you through. “Big names started in a unit just like this one.”",
            "“We sublet floors by the month. When you outgrow your place, ring us.”",
            "Someone's pitching in the elevator. The whole building smells like ambition.",
        ],
    },
    _HOME: {
        "nouns": ["Residences", "Cottage", "Rowhouse", "Lofts", "Terrace",
                  "House", "Quarters", "Flats"],
        "tag": "Residence",
        "greet": [
            "A neighbour waters the stoop. “You run that company downtown? We're all rooting for you.”",
            "Curtains twitch, then a friendly wave. “Keep the noise down after ten and we're square.”",
            "“Morning! Saw your name in the local paper. Small towns talk, you know.”",
            "Just a home. Someone inside is living a quiet life while you build an empire.",
        ],
    },
    _NIGHTLIFE: {
        "nouns": ["Tavern", "Lounge", "Jazz Club", "Alehouse", "Wine Bar",
                  "Pub", "Speakeasy", "Cocktail Room"],
        "tag": "Open late",
        "greet": [
            "Low light, lower music. “First round's on us when you close your seed round.”",
            "“Deals get made at this bar, friend. Pull up a stool sometime.”",
            "A bartender slides over a coaster. “Write your big idea on that. They all start here.”",
            "“We've toasted a few founders into legends. You're welcome anytime.”",
        ],
    },
}

# Which kinds occasionally tip the player a one-time reward, and the band it pays.
# Food/retail/service/nightlife reward the walk-up (loyalty, odd jobs, found
# change); offices and homes are flavour only. ~1 in 3 reward-eligible blocks
# actually pays, so most visits are talk, not an ATM.
_PERK_KINDS = {_FOOD, _RETAIL, _SERVICE, _NIGHTLIFE}
_PERK_LINES = {
    _FOOD: "They press a few bills into your hand. “For the tip jar karma — pay it forward.”",
    _RETAIL: "“Found this in the till with your name on it, basically.” They tip you.",
    _SERVICE: "“Referral bonus — you sent someone our way last week.” A small payout.",
    _NIGHTLIFE: "“You left this on the bar last time, founder.” They slide you some cash.",
}


@dataclass
class Business:
    """A generated tenant for one backdrop block. Stable per (model, x, z)."""
    id: str
    name: str
    kind: str
    model: str         # the scenery GLB it tenants (so labels can find its roof)
    x: float
    z: float
    tag: str           # one-line sub-label under the name ("Open · grab a bite")
    greeting: str      # what the shopfront says on walk-up
    perk: int          # one-time cash reward on first visit (0 = flavour only)
    perk_line: str     # the line shown when the perk pays out
    landmark: str | None = None   # a hand-placed named stop (see LANDMARK_ADDR), else None

    def card_lines(self, paid: bool) -> list[str]:
        """The greeting, plus the perk line the first time (paid=False)."""
        lines = [self.greeting]
        if self.perk and not paid:
            lines.append(self.perk_line)
        return lines


def _hash(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16)


def _kind_for(model: str, h: int) -> str:
    for sub, kind in _MODEL_KIND:
        if sub in model:
            return kind
    return _DEFAULT_KINDS[h % len(_DEFAULT_KINDS)]


def _name_for(kind: str, h: int) -> str:
    nouns = _KINDS[kind]["nouns"]
    noun = nouns[(h // 7) % len(nouns)]
    # ~1 in 4 use an "X & Y" house name instead of a surname, for texture.
    if kind in (_FOOD, _NIGHTLIFE, _RETAIL) and (h // 3) % 4 == 0:
        return f"{_AMPERSAND[(h // 11) % len(_AMPERSAND)]} {noun}"
    return f"{_PROPERS[(h // 13) % len(_PROPERS)]} {noun}"


# --- named landmarks (Layer 2) ----------------------------------------------
# Eight hand-placed tenants that OVERRIDE the procedural business on their grid
# block. Four are "real" read-only surfaces main.py fills from live game state
# (news/museum/realty/library); four are rich-flavour stops with a guaranteed
# one-time perk (diner/gym/apartment/post). main.py routes on `.landmark`.
LANDMARK_ADDR = {
    (9, 6): "news",     (11, 6): "museum",
    (7, 12): "realty",  (13, 12): "library",
    (6, 9): "diner",    (15, 9): "gym",
    (8, 15): "apartment", (12, 15): "post",
}

_LANDMARKS = {
    # name, tag, greeting (real ones' greeting is just the lead-in; main.py adds
    # the live data below it), one-time perk for the flavour stops.
    "news": ("The Daily Ledger", "News stand · today's headlines", "", 0),
    "museum": ("Founders' Hall of Fame", "Museum · your company's story so far", "", 0),
    "realty": ("Keystone Realty", "Real estate · lots around the city", "", 0),
    "library": ("City Library & Patent Office", "Reference desk · free to founders", "", 0),
    "diner": ("The Brass Spoon", "Diner · always open",
              "A red-vinyl booth diner that never closes. “Founders eat on the house "
              "here — coffee bottomless, advice free. Sit as long as you need.”", 40),
    "gym": ("Ironworks Gym", "Fitness · day pass",
            "Clang of iron and a towel tossed your way. “A clear body makes a clear "
            "cap table. Day pass is on us — go blow off some launch-week steam.”", 30),
    "apartment": ("Your Apartment", "Home · rest up",
                  "Your own place above the city. The bed is made, the city lights "
                  "hum below. You sleep like someone who's building something — and the "
                  "game quietly saves while you do.", 50),
    "post": ("City Post & Parcel", "Post office · pick-ups",
             "“Package been waiting on you, founder.” The clerk slides a parcel across "
             "— a rebate cheque from a supplier and a stack of well-wishes from town.", 75),
}
_LANDMARK_PERK_LINE = {
    "diner": "They wave off your wallet and tuck a few bills in your pocket anyway.",
    "gym": "“Locker forty-four had cash in it with no owner. Yours now.”",
    "apartment": "You find some rainy-day savings tucked in a drawer.",
    "post": "The parcel holds a supplier rebate cheque made out to you.",
}


def landmark(sub: str, model: str, x: float, z: float) -> Business:
    """Build the hand-placed landmark `sub` on (model, x, z)."""
    name, tag, greeting, perk = _LANDMARKS[sub]
    return Business(
        id=f"lm_{sub}", name=name, kind="landmark", model=model, x=float(x), z=float(z),
        tag=tag, greeting=greeting, perk=perk, perk_line=_LANDMARK_PERK_LINE.get(sub, ""),
        landmark=sub,
    )


def generate(model: str, x: float, z: float) -> Business:
    """Deterministically build the business standing on (model, x, z)."""
    # round position so tiny placement jitter can't change the identity
    h = _hash(model, round(x, 1), round(z, 1))
    kind = _kind_for(model, h)
    spec = _KINDS[kind]
    name = _name_for(kind, h)
    greeting = spec["greet"][(h // 5) % len(spec["greet"])]
    perk, perk_line = 0, ""
    if kind in _PERK_KINDS and (h // 17) % 3 == 0:      # ~1 in 3 eligible blocks pay
        perk = 20 + (h // 19) % 21 * 5                  # $20..$120 in $5 steps
        perk_line = _PERK_LINES[kind]
    return Business(
        id=f"biz_{int(round(x * 10))}_{int(round(z * 10))}",
        name=name, kind=kind, model=model, x=float(x), z=float(z),
        tag=spec["tag"], greeting=greeting, perk=perk, perk_line=perk_line,
    )

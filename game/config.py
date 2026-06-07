"""Tunable constants for the Company.AI frontend shell.

Characters come from the Kenney "Ultimate Animated Character Pack" (.gltf, each
self-contained with 21 animations). The pack has NO environment/furniture, so the
office floor, walls and desks are drawn from raylib primitives.
"""

WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 720
WINDOW_TITLE = "Company.AI"
TARGET_FPS = 60

# --- Economy (shell-level, replaced by real sim later) ---
# You start from NOTHING: $0, no office, no team. Everything is earned — early
# to-dos pay out a little seed money so you can afford your first lease.
STARTING_CASH = 0
HIRE_COST = 2_000

# --- Office grid (in tiles/metres; characters are ~1.8m tall) ---
TILE = 1.0
GRID_COLS = 16          # x extent
GRID_ROWS = 11          # z extent

# --- Character models (filenames in assets/models/) ---
# The CEO is a business suit; gender just picks the male/female suit body. Both
# expose the same material names (Skin / Hair / Black=jacket), so the onboarding
# tutorial can recolor either one identically.
CEO_MODEL_MALE = "Suit_Male.gltf"
CEO_MODEL_FEMALE = "Suit_Female.gltf"
CEO_MODEL = CEO_MODEL_MALE   # default until the player customizes in onboarding
AGENT_MODELS = [
    "Worker_Male.gltf",
    "Casual_Female.gltf",
    "Doctor_Male_Young.gltf",
    "Casual2_Male.gltf",
    "Worker_Female.gltf",
    "OldClassy_Female.gltf",
    "Casual_Male.gltf",
    "Doctor_Female_Young.gltf",
    "Casual3_Male.gltf",
    "Suit_Female.gltf",
    "Casual2_Female.gltf",
    "OldClassy_Male.gltf",
    "Casual_Bald.gltf",
    "Casual3_Female.gltf",
    "Chef_Male.gltf",
    "Cowboy_Female.gltf",
    "Pirate_Male.gltf",
    "Ninja_Female.gltf",
]

# --- Animation ---
ANIM_IDLE_NAME = "Idle"  # resolved by name per-model (clip order varies)
# The Kenney pack's clips are sampled at 60 fps (e.g. Idle = 246 frames). Playing
# them back any slower makes every character move in sluggish half-speed slow-mo;
# match the authoring rate so walk/idle look natural.
ANIM_FPS = 60.0

# --- Character rendering ---
# Pack models are authored ~3.3 units tall; scale to a ~1.8m human.
CHARACTER_SCALE = 0.55
CHARACTER_NATIVE_HEIGHT = 3.3

# --- Skin tones ---
# The Kenney models expose a flat-colored material literally named "Skin" (no
# textures), so a skin tone is just an RGB tint applied to that material at draw
# time. Tones are a diverse, realistic light->deep range. (r, g, b) in sRGB.
# "Skin" covers the whole head + face + hands (verified by rendering a per-material
# color map), so the skin tone is just this one material. NOTE: the material named
# "Face" is actually the EYES (see EYE_MATERIAL_NAME) — do not tint it as skin.
SKIN_MATERIAL_NAME = "Skin"
SKIN_MATERIAL_NAMES = ("Skin",)
SKIN_TONES = [
    ("Porcelain", (255, 224, 196)),
    ("Fair", (241, 194, 158)),
    ("Medium", (224, 172, 132)),
    ("Tan", (198, 134, 90)),
    ("Brown", (141, 85, 53)),
    ("Deep", (92, 58, 38)),
]

# --- Hair + suit color ---
# Same flat-material trick as skin: the models name their hair mesh "Hair" and
# the suit jacket "Black", so a color choice is just a diffuse tint on that
# material at draw time. Used by the CEO onboarding tutorial (and reusable for
# agents later). (r, g, b) in sRGB.
HAIR_MATERIAL_NAME = "Hair"
HAIR_COLORS = [
    ("Black", (32, 28, 30)),
    ("Brown", (84, 54, 32)),
    ("Auburn", (122, 60, 36)),
    ("Blonde", (206, 170, 102)),
    ("Gray", (164, 164, 170)),
    ("Red", (146, 56, 40)),
]

# Hairstyles for the CEO builder. Every character in the Kenney pack bakes its
# hair into a mesh that uses the "Hair" material, and they all share one skeleton
# — so a "hairstyle" is just: keep the body's own hair (source None), hide it
# (source "bald"), or hide it and draw another character's hair mesh over the head
# (it follows the animation because the skeleton is shared). The borrowed mesh is
# recolored with the chosen HAIR_COLORS. (label, source-model-file).
HAIRSTYLES = [
    ("Default", None),
    ("Short", "Casual_Male.gltf"),
    ("Tousled", "Casual3_Male.gltf"),
    ("Rugged", "Cowboy_Male.gltf"),
    ("Ponytail", "Ninja_Female.gltf"),
    ("Long", "Kimono_Female.gltf"),
    ("Bald", "bald"),
]

SUIT_MATERIAL_NAME = "Black"   # the suit jacket on Suit_Male / Suit_Female
SUIT_COLORS = [
    ("Charcoal", (46, 48, 56)),
    ("Black", (22, 22, 26)),
    ("Navy", (30, 44, 80)),
    ("Slate", (74, 88, 112)),
    ("Burgundy", (88, 32, 42)),
    ("Forest", (34, 58, 44)),
]

# Premium suit colors are locked until purchased (one-time, then reusable). Maps a
# SUIT_COLORS index -> its unlock price. The first three suits are free; the dressier
# three must be bought. Unlock state persists in the shared `unlocked_outfits` set,
# keyed by `suit_outfit_id(idx)`, alongside the marketplace's model outfits.
SUIT_UNLOCKS = {3: 1500, 4: 1500, 5: 1500}


def suit_outfit_id(idx: int) -> str:
    """Stable unlock id for a premium SUIT_COLORS entry (namespaced vs model ids)."""
    return f"suit:{idx}"

# The eyes are their own material — confusingly named "Face" in the Kenney models
# (the actual face skin is "Skin"). This is the one surface we can recolor for eye
# color. First entry is near-white so "normal" eyes are the default.
EYE_MATERIAL_NAME = "Face"
EYE_COLORS = [
    ("White", (245, 245, 238)),
    ("Brown", (96, 60, 34)),
    ("Blue", (74, 122, 190)),
    ("Green", (72, 150, 104)),
    ("Hazel", (140, 100, 46)),
    ("Gray", (122, 130, 138)),
]

# --- Time of day ---
# Real seconds for one full day/night cycle. The world walks through eight
# phases (Midnight->Dawn->Morning->Noon->Afternoon->Dusk->Evening->Night) over
# this span; see game/daylight.py. Press T in-game to peek at the next phase.
# 1200s = 20 real minutes per in-game day (~3 days per real hour).
DAY_SECONDS = 1200.0

# --- Office decor ---
# Seed for the procedural furniture scatter. Same seed -> same office layout
# every launch; change it to reshuffle the plants/cabinets/lounge.
FURNITURE_SEED = 1337

# --- Camera ---
CAM_DISTANCE = 20.0
CAM_HEIGHT = 15.0
CAM_ROTATE_SPEED = 1.4     # radians/sec when holding arrow keys
CAM_ZOOM_SPEED = 14.0
CAM_MIN_DIST = 8.0
CAM_MAX_DIST = 40.0


def grid_to_world(col: float, row: float) -> tuple[float, float]:
    """Convert a grid cell to centered world (x, z) coordinates."""
    x = (col - (GRID_COLS - 1) / 2.0) * TILE
    z = (row - (GRID_ROWS - 1) / 2.0) * TILE
    return x, z

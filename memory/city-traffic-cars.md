---
name: city-traffic-cars
description: Ambient cars drive the park's road grid; real models from the Realistic Car Pack, converted OBJ→GLB with trimesh.
metadata:
  type: project
---

The office park (mode == 'park') has a 20x20 city road grid; ambient traffic
drives it.

- `game/traffic.py` — `Traffic` + `Car`: pure-Python sim (no raylib), drives
  road-to-road on the lattice (intersections at avenue road 1..19 × street road
  1..19), keeps right-hand lane (HALF_LANE = ROAD_W*0.25), prefers straight, no
  immediate U-turn, turns back at edges. Imports grid constants from park.py
  (lazy import in Park.__init__ avoids the cycle). ~26 cars. Unit-test invariant:
  always in-bounds, always on a road centre-line.
- `traffic.py` `VEHICLES` table — drivable fleet: each entry has GLB name, a yaw
  offset (cars/ambulance face +z → 0; Bus/SchoolBus are modelled along +x → 90),
  speed range, fallback-box dims, colour, spawn weight. `Car.vtype` indexes it;
  weighted spawn. Train (too long to turn) + bicycles (distorted) excluded.
- `game/park.py` — `_ModelCache` loads `assets/cars/<Name>.glb` at NATIVE scale
  (VEHICLE_SCALE=1.0, pack≈metres so a bus stays bigger than a car), brightened
  CAR_LIGHT_GAIN=1.4. `_draw_cars` draws GLB via draw_model_ex (c.yaw + v.yaw) or
  an axis-aligned box fallback. `Park.update(dt)` advances traffic; main.py
  `_park_frame` calls it.
- Static street furniture: `_build_props` places TrafficLight on a downtown
  intersection lattice, TrafficSign1-3 on scattered corners, a TrafficCone lane
  near HQ; `_draw_props` renders them (own `_prop_models` cache, culled).

COLOUR GOTCHA — the pack mixes two model variants:
- colour-per-material (NormalCar1/2, SportsCar/2, Cop): 0 UVs, real Kd per part.
- texture-atlas (Taxi, Ambulance, Bus, SchoolBus, Train, bikes, all props):
  have UVs into a shared palette PNG that was NOT copied; their MTLs are flat
  grey Kd 0.64, no map_Kd. So they have no colour data on disk.
Fix in place: `tools/convert_cars.py` bakes each material's Kd into VERTEX colours
(tiled per-vertex, brightened COLOR_GAIN=1.7) — raylib renders vertex colours
(material/texture baseColor came through white). The colour-variant cars get real
colours that way; the atlas ones bake to ~white and are TINTED flat at draw time
(VEHICLES[].tint in traffic.py; PROP_TINT in park.py): taxi/schoolbus yellow, bus
blue, ambulance white, cone orange, etc. Real per-detail colour (incl. a traffic
light's red/amber/green) needs the pack's atlas PNG (a Colormap/Palette .png,
likely in a Textures/ folder by the OBJ) embedded in conversion — not yet done.

Models: Realistic Car Pack (Nov 2018). Vehicles: NormalCar1/2, SportsCar,
SportsCar2, Taxi, Cop, Ambulance, Bus, SchoolBus. Props: TrafficLight,
TrafficSign1/2/3, TrafficCone. (Also converted but unused: Train, Bicycle,
SquareFrameBicycle.) Convert with `tools/convert_cars.py` (trimesh OBJ→GLB;
recentres on origin, sits on y=0, embeds named-colour materials — no texture
maps). Source OBJ+MTL in `assets/cars/source/`. FBX unused (trimesh/raylib can't
read FBX; OBJ covered everything). Buses use yaw 90 — if a bus drives sideways or
rear-first, adjust its `yaw` in VEHICLES.

macOS gotcha: ~/Documents is TCC-protected — Claude Code can't read it even with
the sandbox off (`stat` works, listing = "Operation not permitted"). User had to
copy the pack into the project (Finder / a Terminal window with Documents access),
not via the in-session `!` (same process, same block).

ROADS — the drawn asphalt was replaced by a modular street-tile pack
(`assets/city/OBJ` → `assets/city/glb`, converted by the same script with a
src/out arg: `convert_cars.py assets/city/OBJ assets/city/glb`). Tiles are 2x2
Kenney-style (White lines / Black asphalt / Grey sidewalk). park.py `_draw_streets`
now places `Street_4Way` at every road intersection, scaled STREET_XZ=BLOCK/2 in
XZ (one tile = one block, corners land on building addresses) and flattened in Y
(STREET_Y=0.5, STREET_Y_OFF=-0.1). Loaded via `_street_models = _ModelCache(base=
CITY_DIR)`; culled at TILE_CULL=46. Falls back to `_draw_streets_flat` (old drawn
asphalt) if the GLB is missing. Pack also has Straight/Curve/3Way/Deadend/Bridge
tiles (unused — could do proper edge tiles instead of all-4way).

Street furniture now comes from the street pack too (park.py `_build_props`/
`_draw_props`, entries are (source, name, x, z, yaw, scale); source 'city' uses
`_street_models`, 'cars' uses `_prop_models`): Streetlight_Single lines the grid
(every 2nd intersection), city TrafficLight (real green/amber/red lenses) at
downtown intersections, Sign_Stop/NoParking/Triangle on scattered corners, all at
CITY_PROP_SCALE=6; the car-pack TrafficCone keeps the HQ coned lane. Replaced the
old car-pack TrafficLight/TrafficSign props.

Not done: cars pass through each other and the player (no collision / stop-at-
intersection); speeds 5.5–8.5 u/s; road tiles are all 4-way (edges stub off-grid,
no curves/3-ways at borders). See [[company-ai-project]].

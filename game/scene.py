"""3D scene: a primitive office (the character pack has no environment assets),
an orbital camera, and character rendering."""
from __future__ import annotations

import math
import pyray as pr

from . import config, furniture, zones, floorplan

FLOOR_COLOR = pr.Color(228, 230, 238, 255)
WALL_COLOR = pr.Color(208, 212, 224, 255)
DOOR_COLOR = pr.Color(150, 105, 70, 255)
DESK_COLOR = pr.Color(140, 105, 70, 255)
DESK_TOP_COLOR = pr.Color(170, 130, 90, 255)


def _color(rgb) -> pr.Color:
    """Accept an (r,g,b) sequence or an existing pr.Color; return a pr.Color."""
    if isinstance(rgb, (list, tuple)):
        return pr.Color(int(rgb[0]), int(rgb[1]), int(rgb[2]), 255)
    return rgb


class Scene:
    def __init__(self, plan: floorplan.FloorPlan | None = None) -> None:
        self.cam_angle = math.radians(35.0)
        self.cam_distance = config.CAM_DISTANCE
        self.camera = pr.Camera3D(
            pr.Vector3(0, config.CAM_HEIGHT, config.CAM_DISTANCE),
            pr.Vector3(0, 1.0, 0),
            pr.Vector3(0, 1.0, 0),
            45.0,
            pr.CAMERA_PERSPECTIVE,
        )
        self.floor_color = FLOOR_COLOR                  # repaintable via the shop
        self.wall_color = WALL_COLOR
        self.door_color = DOOR_COLOR
        self.show_records = True                         # the Company Files cabinet (your office only)
        self.show_ceo_desk = False                       # the CEO Desk (only in the CEO's office room)
        self.set_plan(plan or floorplan.DEFAULT_HQ)
        self._update_camera_position()

    def set_plan(self, plan: floorplan.FloorPlan, seed: int | None = None) -> None:
        """Switch the interior to a floor plan: resize the room and regenerate its
        ambient decor (shop-bought props don't carry between buildings). `seed`
        (per-room) varies the decor so same-template wings still look different."""
        self.plan = plan
        self._floor = (plan.cols * config.TILE, plan.rows * config.TILE)
        self._furniture = furniture.generate_layout(
            plan.furniture_seed if seed is None else seed, plan.cols, plan.rows)
        # The Company Records cabinet: a fixed, interactive filing cabinet against
        # the left wall (walk up + E opens the Company Dossier). Same in every room.
        w, d = self._floor
        self._records_pos = (-w / 2 + 0.5, -d * 0.05)
        # The CEO Desk: a fixed executive desk against the back wall (left of the
        # door, which sits at +w*0.28), facing +z into the room. Walk up + E opens
        # the Global AI Terminal. Only drawn when show_ceo_desk is set (the CEO's
        # own office room — see main._activate_room).
        self._ceo_desk_pos = (-w * 0.26, -d / 2 + 1.5)

    def set_floor_color(self, rgb) -> None:
        self.floor_color = _color(rgb)

    def set_wall_color(self, rgb) -> None:
        self.wall_color = _color(rgb)

    def set_door_color(self, rgb) -> None:
        self.door_color = _color(rgb)

    # -- camera --------------------------------------------------------------
    def _update_camera_position(self) -> None:
        x = math.sin(self.cam_angle) * self.cam_distance
        z = math.cos(self.cam_angle) * self.cam_distance
        self.camera.position = pr.Vector3(x, config.CAM_HEIGHT, z)

    def update(self, dt: float) -> None:
        if pr.is_key_down(pr.KEY_LEFT):
            self.cam_angle -= config.CAM_ROTATE_SPEED * dt
        if pr.is_key_down(pr.KEY_RIGHT):
            self.cam_angle += config.CAM_ROTATE_SPEED * dt
        if pr.is_key_down(pr.KEY_UP):
            self.cam_distance -= config.CAM_ZOOM_SPEED * dt
        if pr.is_key_down(pr.KEY_DOWN):
            self.cam_distance += config.CAM_ZOOM_SPEED * dt
        self.cam_distance -= pr.get_mouse_wheel_move() * 2.0
        self.cam_distance = max(config.CAM_MIN_DIST, min(config.CAM_MAX_DIST, self.cam_distance))
        self._update_camera_position()

    # -- world ---------------------------------------------------------------
    def _draw_office(self) -> None:
        w, d = self._floor
        pr.draw_plane(pr.Vector3(0, 0, 0), pr.Vector2(w, d), self.floor_color)
        pr.draw_grid(max(self.plan.cols, self.plan.rows), config.TILE)

        # Back + side walls (thin slabs around the floor)
        h = 2.6
        pr.draw_cube(pr.Vector3(0, h / 2, -d / 2), w, h, 0.15, self.wall_color)      # back
        pr.draw_cube(pr.Vector3(-w / 2, h / 2, 0), 0.15, h, d, self.wall_color)      # left
        pr.draw_cube(pr.Vector3(w / 2, h / 2, 0), 0.15, h, d, self.wall_color)       # right
        self._draw_door(w, d, h)
        self._draw_meeting_area()
        self._draw_lounges()
        self._draw_fixtures(d)
        if self.show_records:
            self._draw_records_cabinet(*self._records_pos)
        if self.show_ceo_desk:
            self._draw_ceo_desk(*self._ceo_desk_pos)

    def _draw_door(self, w: float, d: float, wall_h: float) -> None:
        """A door set into the back wall (slightly proud of it), with a frame and
        handle. Recolorable via the shop."""
        dw, dh = 1.3, 2.2
        x, z = w * 0.28, -d / 2 + 0.02           # offset from centre, just in front of wall
        frame = pr.Color(60, 62, 70, 255)
        pr.draw_cube(pr.Vector3(x, dh / 2, z), dw + 0.16, dh + 0.12, 0.06, frame)    # frame
        pr.draw_cube(pr.Vector3(x, dh / 2, z + 0.03), dw, dh, 0.06, self.door_color)  # slab
        pr.draw_cube(pr.Vector3(x + dw * 0.36, 1.05, z + 0.08), 0.08, 0.08, 0.06,    # handle
                     pr.Color(225, 205, 120, 255))

    def _draw_meeting_area(self) -> None:
        """A round conference table + ring of stools at every meeting zone in the
        active plan (a plan may have several)."""
        wood = pr.Color(150, 110, 74, 255)
        leg = pr.Color(40, 42, 50, 255)
        seat = pr.Color(70, 76, 92, 255)
        for cx, cz in zones.meeting_centers():
            # table: a pedestal + a round top
            pr.draw_cylinder(pr.Vector3(cx, 0.0, cz), 0.12, 0.18, 0.72, 12, leg)
            pr.draw_cylinder(pr.Vector3(cx, 0.72, cz), 0.72, 0.72, 0.07, 24, wood)
            pr.draw_cylinder(pr.Vector3(cx, 0.72, cz), 0.74, 0.74, 0.02, 24,
                             pr.Color(120, 88, 60, 255))
            # stools (orientation-free, so they read right from any angle)
            for sx, sz in zones.meeting_seats((cx, cz)):
                pr.draw_cylinder(pr.Vector3(sx, 0.0, sz), 0.05, 0.07, 0.44, 8, leg)
                pr.draw_cylinder(pr.Vector3(sx, 0.44, sz), 0.22, 0.22, 0.07, 12, seat)

    def _draw_fixtures(self, depth: float) -> None:
        """Per-room fixtures keyed off the plan's zones: a reception desk in the
        lobby, elevator doors wherever there's an elevator, and a flight of stairs
        plus a bed in a home."""
        for z in self.plan.zones:
            x, zz = self.plan.grid_to_world(z.col, z.row)
            if z.kind == "reception":
                self._draw_reception(x, zz)
            elif z.kind == "elevator":
                self._draw_elevator_doors(x, zz, depth)
            elif z.kind == "stairs":
                self._draw_stairs(x, zz)
            elif z.kind == "bed":
                self._draw_bed(x, zz)

    def _draw_stairs(self, x: float, z: float) -> None:
        """A wooden flight of stairs climbing toward the back wall (-z), with a small
        upper landing and a stepped banister down the open (+x) side. Every home has
        one — it's the feature that makes a room read as a home, not an office."""
        wood = pr.Color(150, 110, 74, 255)
        riser = pr.Color(120, 88, 60, 255)
        rail = pr.Color(96, 72, 50, 255)
        steps, sh, sd, sw = 7, 0.19, 0.34, 1.5
        for i in range(steps):
            cy = sh / 2 + i * sh
            cz = z - i * sd                                   # march up toward the wall
            pr.draw_cube(pr.Vector3(x, cy, cz), sw, sh, sd, wood)            # tread
            pr.draw_cube(pr.Vector3(x, cy - sh / 2, cz - sd / 2), sw, i * sh + sh, 0.04, riser)
            pr.draw_cube_wires(pr.Vector3(x, cy, cz), sw, sh, sd, pr.Color(0, 0, 0, 55))
            # banister post + cap on the open side, rising with the steps
            px = x + sw / 2 - 0.06
            pr.draw_cube(pr.Vector3(px, cy + 0.45, cz), 0.06, 0.9, 0.06, rail)
            pr.draw_cube(pr.Vector3(px, cy + 0.9, cz), 0.1, 0.06, sd, rail)
        # upper landing the stairs arrive on
        top_y = steps * sh
        top_z = z - steps * sd
        pr.draw_cube(pr.Vector3(x, top_y - sh / 2, top_z - sd * 0.7), sw, sh, sd * 1.8, wood)
        pr.draw_cube_wires(pr.Vector3(x, top_y - sh / 2, top_z - sd * 0.7), sw, sh, sd * 1.8,
                           pr.Color(0, 0, 0, 55))

    def _draw_bed(self, x: float, z: float) -> None:
        """A made bed (frame + mattress + blanket + pillow + headboard). The head is
        at -z, against the back wall, so it faces into the room."""
        frame = pr.Color(120, 92, 64, 255)
        mattress = pr.Color(232, 234, 240, 255)
        blanket = pr.Color(70, 110, 170, 255)
        pillow = pr.Color(246, 246, 250, 255)
        bw, bl = 1.7, 2.3                                     # width (x), length (z)
        pr.draw_cube(pr.Vector3(x, 0.22, z), bw, 0.3, bl, frame)              # frame base
        pr.draw_cube(pr.Vector3(x, 0.46, z), bw - 0.1, 0.2, bl - 0.1, mattress)  # mattress
        pr.draw_cube(pr.Vector3(x, 0.52, z + bl * 0.16), bw - 0.1, 0.12, bl * 0.62, blanket)  # blanket
        pr.draw_cube(pr.Vector3(x, 0.58, z - bl / 2 + 0.42), bw - 0.55, 0.16, 0.55, pillow)   # pillow
        pr.draw_cube(pr.Vector3(x, 0.62, z - bl / 2 - 0.04), bw, 0.74, 0.1, frame)            # headboard

    def _draw_reception(self, x: float, z: float) -> None:
        body = pr.Color(120, 92, 64, 255)
        pr.draw_cube(pr.Vector3(x, 0.55, z), 2.0, 1.1, 0.6, body)            # counter front
        pr.draw_cube(pr.Vector3(x, 1.12, z), 2.1, 0.08, 0.72, pr.Color(170, 130, 90, 255))  # top
        pr.draw_cube(pr.Vector3(x, 0.55, z - 0.34), 2.0, 1.1, 0.08, pr.Color(96, 74, 52, 255))  # back panel
        pr.draw_cube(pr.Vector3(x, 1.7, z - 0.34), 1.0, 0.5, 0.06, pr.Color(70, 110, 170, 255))  # sign board

    def _draw_elevator_doors(self, x: float, z: float, depth: float) -> None:
        # Doors sit flush against the back wall, just behind the elevator zone.
        bz = -depth / 2 + 0.14
        frame = pr.Color(60, 64, 74, 255)
        metal = pr.Color(150, 158, 170, 255)
        pr.draw_cube(pr.Vector3(x, 1.15, bz), 1.7, 2.3, 0.12, frame)         # frame
        for ox in (-0.39, 0.39):
            pr.draw_cube(pr.Vector3(x + ox, 1.08, bz + 0.08), 0.72, 2.04, 0.05, metal)
        pr.draw_cube(pr.Vector3(x, 2.42, bz), 1.2, 0.22, 0.08, pr.Color(90, 150, 230, 255))  # call light

    def _draw_records_cabinet(self, x: float, z: float) -> None:
        """The Company Records cabinet — a tall, distinct navy filing cabinet with a
        gold top and a labelled folder tab, so it reads as the 'open the dossier'
        spot rather than ambient decor. Drawers face +z (into the room)."""
        body = pr.Color(48, 70, 120, 255)
        gold = pr.Color(210, 175, 90, 255)
        dh, n = 0.40, 4
        for i in range(n):
            cy = dh / 2 + i * dh
            pr.draw_cube(pr.Vector3(x, cy, z), 0.84, dh - 0.03, 0.62, body)
            pr.draw_cube_wires(pr.Vector3(x, cy, z), 0.84, dh - 0.03, 0.62, pr.Color(0, 0, 0, 90))
            pr.draw_cube(pr.Vector3(x, cy, z + 0.32), 0.26, 0.05, 0.03, gold)   # handle
        top = n * dh
        pr.draw_cube(pr.Vector3(x, top + 0.02, z), 0.9, 0.06, 0.68, gold)        # lid
        # A standing folder/placard on top so it's identifiable from across the room.
        pr.draw_cube(pr.Vector3(x, top + 0.27, z - 0.05), 0.5, 0.36, 0.04,
                     pr.Color(235, 226, 200, 255))
        pr.draw_cube(pr.Vector3(x, top + 0.42, z - 0.07), 0.22, 0.1, 0.04, gold)  # folder tab

    def records_pos(self) -> tuple[float, float]:
        """World (x, z) of the Company Records cabinet (for the walk-up prompt)."""
        return self._records_pos

    def ceo_desk_pos(self) -> tuple[float, float]:
        """World (x, z) of the CEO Desk (for the walk-up prompt)."""
        return self._ceo_desk_pos

    def _draw_ceo_desk(self, x: float, z: float) -> None:
        """The CEO's power desk: a wide dark executive desk with a black glass top,
        an exec chair behind it, and a glowing green terminal monitor on top — so it
        reads, from across the room, as 'the computer you run the company from'.
        The desk faces +z (into the room); the CEO uses it from the front."""
        wood = pr.Color(58, 44, 34, 255)        # dark mahogany body
        glass = pr.Color(24, 26, 32, 255)       # smoked glass top
        trim = pr.Color(210, 175, 90, 255)      # brass trim
        dw, dd = 2.2, 0.95                       # desk width / depth
        # Body (modesty panel) + brass kickplate.
        pr.draw_cube(pr.Vector3(x, 0.37, z), dw, 0.74, dd, wood)
        pr.draw_cube(pr.Vector3(x, 0.06, z + dd / 2 - 0.02), dw, 0.12, 0.04, trim)
        # Glass top with a thin brass edge.
        pr.draw_cube(pr.Vector3(x, 0.76, z), dw + 0.12, 0.06, dd + 0.12, glass)
        pr.draw_cube_wires(pr.Vector3(x, 0.76, z), dw + 0.12, 0.06, dd + 0.12, trim)

        # Monitor toward the back of the desk, screen facing the CEO (+z).
        mz = z - 0.22
        pr.draw_cube(pr.Vector3(x, 0.92, mz), 0.10, 0.16, 0.10, pr.Color(20, 20, 24, 255))  # stand
        pr.draw_cube(pr.Vector3(x, 1.22, mz), 1.16, 0.66, 0.06, pr.Color(16, 18, 20, 255))  # bezel
        # Glowing terminal screen (pulses subtly so it clearly reads as "on").
        pulse = 150 + int(60 * (0.5 + 0.5 * math.sin(pr.get_time() * 2.0)))
        screen = pr.Color(30, min(255, pulse), 70, 255)
        pr.draw_cube(pr.Vector3(x, 1.22, mz + 0.035), 1.04, 0.54, 0.02, screen)
        pr.draw_cube(pr.Vector3(x, 1.22, mz + 0.045), 1.04, 0.54, 0.005,
                     pr.Color(120, 255, 150, 90))   # phosphor bloom
        # Keyboard slab on the glass, CEO side.
        pr.draw_cube(pr.Vector3(x, 0.80, z + 0.18), 0.7, 0.04, 0.24, pr.Color(40, 44, 50, 255))

        # Executive chair behind the desk (back to the wall, seat facing +z).
        cz = z - 0.7
        pr.draw_cylinder(pr.Vector3(x, 0.0, cz), 0.06, 0.08, 0.46, 8, pr.Color(30, 32, 38, 255))
        pr.draw_cube(pr.Vector3(x, 0.50, cz), 0.56, 0.10, 0.52, pr.Color(48, 52, 60, 255))   # seat
        pr.draw_cube(pr.Vector3(x, 0.86, cz - 0.24), 0.56, 0.72, 0.10, pr.Color(40, 44, 52, 255))  # backrest

    def _draw_lounges(self) -> None:
        """A rug + couch at every lounge zone in the active plan (where bots sit
        to relax). Couch faces +z, matching the lounge seat the game assigns."""
        for lx, lz in zones.lounge_points():
            furniture.draw_lounge(lx, lz)

    def _draw_desk(self, x: float, z: float) -> None:
        pr.draw_cube(pr.Vector3(x, 0.75, z), 1.4, 0.08, 0.8, DESK_TOP_COLOR)        # top
        for ox, oz in ((-0.6, -0.3), (0.6, -0.3), (-0.6, 0.3), (0.6, 0.3)):
            pr.draw_cube(pr.Vector3(x + ox, 0.37, z + oz), 0.08, 0.74, 0.08, DESK_COLOR)

    def add_prop(self, prop) -> None:
        """Append a procedurally-built prop (e.g. a shop purchase) to the office."""
        self._furniture.append(prop)

    def furniture(self) -> list:
        """The current furniture props (so the navgrid can mark their footprints)."""
        return self._furniture

    def _draw_selection_ring(self, ch) -> None:
        center = pr.Vector3(ch.x, 0.02, ch.z)  # just above the floor
        rot = pr.Vector3(1, 0, 0)               # lay the ring flat on the ground
        for r in (0.55, 0.62):
            pr.draw_circle_3d(center, r, rot, 90.0, pr.Color(70, 200, 120, 255))

    def draw_world(self, characters, registry, camera, selected=None) -> None:
        pr.begin_mode_3d(camera)
        self._draw_office()
        for prop in self._furniture:                 # procedural decor
            furniture.draw_prop(prop)
        for ch in characters:
            if ch.desk is not None:
                self._draw_desk(ch.desk[0], ch.desk[1])
                # Chair at the worker's seat (in front of the desk), backrest on the
                # far side from the desk so the seated worker faces their desk.
                seat = ch.seat or (ch.desk[0], ch.desk[1] - 0.55)
                face = -1 if seat[1] > ch.desk[1] else 1
                furniture.draw_desk_chair(seat[0], seat[1], face=face)
        if selected is not None:
            self._draw_selection_ring(selected)
        for ch in characters:
            ch.draw(registry)
        pr.end_mode_3d()

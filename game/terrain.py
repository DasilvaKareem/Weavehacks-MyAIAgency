"""3D terrain for the city: a flat basin where the road grid lives, ramping up to
rolling hills/mountains around and beyond it.

The city's roads/sidewalks are flat quads covering the whole block grid, so the
terrain is held FLAT (at `baseline`) within `flat_radius` of the origin and only
rises outside it — anything placed out there (backdrop buildings, trees, props,
wandering characters) reads its ground height from `height_at(x, z)`.

Heights come from smooth value-noise (fBm) by default, so no asset is required.
Drop a grayscale PNG at assets/heightmap.png (white = high) to drive the shape
from a real map instead — same flat-basin masking is applied on top.
"""
from __future__ import annotations

import math
import os

import pyray as pr

HEIGHTMAP_PNG = os.path.join(os.path.dirname(__file__), os.pardir, "assets", "heightmap.png")


def _smoothstep(a: float, b: float, x: float) -> float:
    if x <= a:
        return 0.0
    if x >= b:
        return 1.0
    t = (x - a) / (b - a)
    return t * t * (3.0 - 2.0 * t)


class Terrain:
    def __init__(self, *, span: float, flat_radius: float, city_radius: float,
                 baseline: float = -0.08, max_height: float = 46.0, res: int = 168,
                 ramp: float = 90.0, seed: int = 7, sea_drop: float = 10.0,
                 coast_start: float = 170.0, city_amp: float = 1.3) -> None:
        """span: full width/depth of the terrain (>= the city span, usually ~2x so
        hills extend past the skyline). flat_radius: distance from origin kept dead
        flat for the road grid. city_radius: flat area drawn as concrete (the city
        floor) rather than grass. ramp: how far the flat basin blends up into hills.
        res: mesh grid resolution per side."""
        self.span = span
        self.flat_radius = flat_radius
        self.city_radius = city_radius
        self.baseline = baseline
        self.max_height = max_height
        self.ramp = ramp
        self.res = res
        self.sea_drop = sea_drop
        self.sea_y = baseline - sea_drop      # the sea surface height
        self.coast_start = coast_start        # how far west (-x) the coast begins to roll down
        self.city_amp = city_amp              # low-amplitude roll over the whole map (incl. city)
        self._seed = seed
        self._png = self._load_png()            # optional (W,H,[0..1]) grayscale
        self._half = span / 2.0
        self._model = None                       # built lazily once GL ctx exists

    # --- height field ------------------------------------------------------
    def _load_png(self):
        if not os.path.exists(HEIGHTMAP_PNG):
            return None
        img = pr.load_image(HEIGHTMAP_PNG)
        w, h = img.width, img.height
        cols = pr.load_image_colors(img)
        grays = [0.0] * (w * h)
        for i in range(w * h):
            c = cols[i]
            grays[i] = (c.r + c.g + c.b) / (3.0 * 255.0)
        pr.unload_image_colors(cols)
        pr.unload_image(img)
        return (w, h, grays)

    def _noise(self, x: float, z: float) -> float:
        """Smooth fBm value-noise in [0,1] from summed, phase-shifted sinusoids —
        cheap, seamless, and good enough for rolling hills (no asset needed)."""
        s = self._seed * 0.123
        v = 0.0
        amp = 1.0
        tot = 0.0
        freq = 0.0065
        for _ in range(4):
            v += amp * (
                math.sin(x * freq + s) * math.cos(z * freq * 1.3 - s)
                + math.sin((x + z) * freq * 0.7 + s * 2.0)
            )
            tot += amp
            amp *= 0.5
            freq *= 2.0
        return max(0.0, min(1.0, (v / (2.0 * tot)) + 0.5))

    def _raw_height(self, x: float, z: float) -> float:
        """Unmasked hill height [0..max_height] from the PNG if present, else noise."""
        if self._png is not None:
            w, h, g = self._png
            u = (x + self._half) / self.span * (w - 1)
            t = (z + self._half) / self.span * (h - 1)
            x0 = max(0, min(w - 1, int(u)))
            z0 = max(0, min(h - 1, int(t)))
            x1 = min(w - 1, x0 + 1)
            z1 = min(h - 1, z0 + 1)
            fx, fz = u - x0, t - z0
            top = g[z0 * w + x0] * (1 - fx) + g[z0 * w + x1] * fx
            bot = g[z1 * w + x0] * (1 - fx) + g[z1 * w + x1] * fx
            gray = top * (1 - fz) + bot * fz
        else:
            gray = self._noise(x, z)
        return gray * self.max_height

    def _coast_height(self, x: float, z: float) -> float:
        """West-side profile (rolling hills -> sand -> sea): rolling hills just west
        of the city ease down through a wiggly beach to below the sea surface, then a
        gentle seabed."""
        d = (-x) - self.coast_start                  # distance west of where the coast begins
        beach = 95.0 + 30.0 * math.sin(z * 0.012 + 1.3)    # wiggly shoreline, not a ruler line
        if d < beach:
            f = d / beach                            # 0 at coast start -> 1 at waterline
            hills = self._noise(x, z) * 16.0 * (1.0 - f)    # rolling hills, flattening to the beach
            return self.baseline + hills - (self.baseline - self.sea_y) * (f * f)
        return self.sea_y - 2.5 - (d - beach) * 0.03  # seabed sloping away under the water

    def _city_roll(self, x: float, z: float) -> float:
        """A gentle, smooth, low-frequency roll applied across the whole map (the city
        included) so the ground isn't dead flat. Low amplitude + low frequency means
        only ~tenths of a unit change per block, so the flat road tiles step softly
        and car/character navigation (which is X/Z only) is unaffected."""
        return (math.sin(x * 0.017 + 0.7) * math.cos(z * 0.020 - 0.4)
                + 0.5 * math.sin((x + z) * 0.011)) * self.city_amp

    def height_at(self, x: float, z: float) -> float:
        """World ground height at (x, z): a gentle roll everywhere, rising into hills
        to the north/east/south, with a rolling-hills/beach/sea descent on the west."""
        # West coastline takes priority: anything west of coast_start rolls to the sea.
        west = _smoothstep(self.coast_start, self.coast_start + 55.0, -x)
        if west > 0.0:
            return self.baseline * (1.0 - west) + self._coast_height(x, z) * west
        roll = self._city_roll(x, z)
        r = math.hypot(x, z)
        t = _smoothstep(self.flat_radius, self.flat_radius + self.ramp, r)
        if t <= 0.0:
            return self.baseline + roll
        return self.baseline + roll + t * self._raw_height(x, z)

    # --- mesh --------------------------------------------------------------
    def _color_at(self, y_above: float, slope: float, radius: float):
        """City floor reads as concrete; outside it grass low -> rock -> snow high,
        darkened on steep slopes."""
        # Coastline: only the deep west descent (well below the gentle city roll) is
        # beach/sea — the city's own ±roll must not read as sand.
        if y_above < -3.5:
            if y_above > -self.sea_drop:     # above the waterline -> dry/wet beach sand
                return (216, 200, 156)
            return (122, 130, 120)           # submerged seabed (reads under the blue water)
        # City floor: concrete within the city radius (tolerant of the gentle roll).
        if radius < self.city_radius and y_above < 4.0:
            return (176, 178, 184)       # concrete, matches the old flat ground plane
        h = min(1.0, max(0.0, y_above) / self.max_height)
        if h < 0.33:
            r, g, b = 96, 138, 74        # grass
        if h < 0.33:
            r, g, b = 96, 138, 74        # grass
        elif h < 0.7:
            k = (h - 0.33) / 0.37
            r = int(96 + k * 40); g = int(138 - k * 50); b = int(74 + k * 6)   # -> rock
        else:
            k = min(1.0, (h - 0.7) / 0.3)
            r = int(136 + k * 100); g = int(88 + k * 140); b = int(80 + k * 150)  # -> snow
        shade = 1.0 - 0.45 * min(1.0, slope)
        return (int(r * shade), int(g * shade), int(b * shade))

    def build(self) -> None:
        """Build the colored terrain mesh (needs a GL context). Sampled from the SAME
        height_at() the rest of the game uses, so objects sit exactly on the surface."""
        if self._model is not None:
            return
        n = self.res
        step = self.span / (n - 1)
        h0 = -self._half
        # grid of heights
        H = [[0.0] * n for _ in range(n)]
        for j in range(n):
            z = h0 + j * step
            for i in range(n):
                x = h0 + i * step
                H[j][i] = self.height_at(x, z)

        vcount = n * n
        tcount = (n - 1) * (n - 1) * 2
        verts = pr.ffi.new("float[]", vcount * 3)
        norms = pr.ffi.new("float[]", vcount * 3)
        cols = pr.ffi.new("unsigned char[]", vcount * 4)
        idx = pr.ffi.new("unsigned short[]", tcount * 3)

        for j in range(n):
            z = h0 + j * step
            for i in range(n):
                x = h0 + i * step
                k = j * n + i
                y = H[j][i]
                verts[k * 3 + 0] = x
                verts[k * 3 + 1] = y
                verts[k * 3 + 2] = z
                # normal from central differences
                hl = H[j][i - 1] if i > 0 else y
                hr = H[j][i + 1] if i < n - 1 else y
                hd = H[j - 1][i] if j > 0 else y
                hu = H[j + 1][i] if j < n - 1 else y
                nx, ny, nz = (hl - hr), 2.0 * step, (hd - hu)
                inv = 1.0 / math.sqrt(nx * nx + ny * ny + nz * nz)
                norms[k * 3 + 0] = nx * inv
                norms[k * 3 + 1] = ny * inv
                norms[k * 3 + 2] = nz * inv
                slope = 1.0 - ny * inv          # 0 flat -> ~1 steep
                r, g, b = self._color_at(y - self.baseline, slope, math.hypot(x, z))
                cols[k * 4 + 0] = r
                cols[k * 4 + 1] = g
                cols[k * 4 + 2] = b
                cols[k * 4 + 3] = 255

        t = 0
        for j in range(n - 1):
            for i in range(n - 1):
                a = j * n + i
                b = a + 1
                c = a + n
                d = c + 1
                for v in (a, c, b, b, c, d):     # two CCW tris, up-facing
                    idx[t] = v
                    t += 1

        mesh = pr.Mesh()
        mesh.vertexCount = vcount
        mesh.triangleCount = tcount
        mesh.vertices = verts
        mesh.normals = norms
        mesh.colors = cols
        mesh.indices = idx
        self._keep = (verts, norms, cols, idx)   # keep cffi buffers alive
        pr.upload_mesh(pr.ffi.addressof(mesh), False)
        self._model = pr.load_model_from_mesh(mesh)

    def draw(self) -> None:
        if self._model is None:
            self.build()
        pr.draw_model(self._model, pr.Vector3(0, 0, 0), 1.0, pr.WHITE)
        # One flat sea at sea level across the whole map. Land sits above it (occludes
        # it); only the west coast dips below, so the sea shows exactly there.
        pr.draw_plane(pr.Vector3(0, self.sea_y, 0), pr.Vector2(self.span, self.span),
                      pr.Color(54, 118, 168, 255))

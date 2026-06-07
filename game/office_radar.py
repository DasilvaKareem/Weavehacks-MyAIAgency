"""Office Radar — a live, Redis-fed map of who's in which office room.

Press K to toggle. The data is a snapshot the game keeps fresh from the Redis
geospatial index (backend/city_geo.py): every agent + the CEO, grouped by their
office room. It's the SAME live geo the CEO-desk terminal queries with
who_is_in_room / team_map — here it's drawn as a radar so you can see the whole
company at a glance. Offline-safe: with no Redis it just says so, and it never
touches the network itself (the snapshot is fetched off-thread by the game).
"""
import pyray as pr

_BG = pr.Color(12, 18, 28, 236)
_BORDER = pr.Color(60, 90, 130, 255)
_HEAD = pr.Color(120, 210, 255, 255)
_ROOM = pr.Color(255, 215, 120, 255)
_DIM = pr.Color(150, 165, 185, 255)


def _role_color(role: str, kind: str) -> pr.Color:
    if kind == "building":
        return pr.Color(140, 150, 165, 255)
    r = (role or "").lower()
    if "ceo" in r:
        return pr.GOLD
    if "engineer" in r or "develop" in r:
        return pr.Color(120, 200, 255, 255)
    if "design" in r:
        return pr.Color(220, 150, 255, 255)
    if "sales" in r or "market" in r:
        return pr.Color(120, 235, 150, 255)
    if "research" in r or "analyst" in r or "data" in r:
        return pr.Color(255, 180, 120, 255)
    return pr.Color(200, 210, 225, 255)


class OfficeRadarPanel:
    def __init__(self) -> None:
        self.open = False

    def toggle(self) -> None:
        self.open = not self.open

    def close(self) -> None:
        self.open = False

    def draw(self, snapshot: list, configured: bool = True) -> None:
        """Render the radar from a room-summary snapshot:
        [{room, members:[{name, role, kind}]}]. Shops ('City') are dropped."""
        if not self.open:
            return
        W, H = 460, 560
        x, y = 24, 84
        pr.draw_rectangle(x, y, W, H, _BG)
        pr.draw_rectangle_lines(x, y, W, H, _BORDER)
        pr.draw_text("OFFICE RADAR", x + 18, y + 16, 26, _HEAD)
        pr.draw_text("live from Redis geo  ·  press K to close", x + 18, y + 47, 14, _DIM)

        groups = [g for g in (snapshot or []) if g.get("room") != "City"]
        cy = y + 80
        if not configured:
            pr.draw_text("offline — set REDIS_URL to light this up", x + 18, cy, 18, _DIM)
            return
        if not groups:
            pr.draw_text("no positions yet…", x + 18, cy, 18, _DIM)
            return

        total = sum(len(g["members"]) for g in groups)
        pr.draw_text(f"{total} on the floor  ·  {len(groups)} rooms",
                     x + 18, y + 64, 13, _DIM)
        for g in groups:
            if cy > y + H - 30:
                break
            pr.draw_text(f"{g['room']}  ({len(g['members'])})", x + 18, cy, 19, _ROOM)
            cy += 27
            for m in g["members"]:
                if cy > y + H - 22:
                    break
                pr.draw_circle(x + 30, cy + 8, 5, _role_color(m.get("role"), m.get("kind")))
                name = m.get("name", "?")
                pr.draw_text(name, x + 44, cy, 16, pr.RAYWHITE)
                sub = m.get("role") or m.get("kind") or ""
                if sub:
                    nw = pr.measure_text(name, 16)
                    pr.draw_text(f"· {sub}", x + 44 + nw + 8, cy + 1, 14, _DIM)
                cy += 22
            cy += 10

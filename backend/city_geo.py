"""The city as a Redis geospatial index — where everyone is, answered in O(log n).

Company.AI is a living 3D city: agents, NPCs and buildings all have a spot on the
ground plane. This keeps that map in Redis (GEOADD) so anything can ask spatial
questions at Redis speed: who's near the cafe? where's the nearest idle engineer?
which agents are within shouting distance of the CEO? GEOSEARCH answers in
milliseconds without the game loop scanning every entity every frame.

Game space → geo space: the world's (x, z) ground coordinates are mapped onto a
tiny patch of lon/lat around the origin (1 game unit ≈ 1 metre), so GEODIST comes
back in game units and proximity ordering is exact.

Graceful: no REDIS_URL → every call is a safe no-op; the game runs unchanged.
"""
from __future__ import annotations

import json
import logging

from .agent_bus import _ns, _redis

log = logging.getLogger("company.geo")

# 1 degree of latitude ≈ 111_320 m. Dividing game units by this lands the whole
# city in a sub-degree patch near (0,0), where lon/lat are locally linear, so
# GEODIST(metres) ≈ game-unit distance and ranking is preserved.
_DEG = 111_320.0


def is_configured() -> bool:
    from .agent_bus import is_configured as redis_on

    return redis_on()


def _city() -> str:
    return f"{_ns()}:geo:city"


def _meta() -> str:
    return f"{_ns()}:geo:meta"


def _to_lonlat(x: float, z: float) -> tuple[float, float]:
    return (x / _DEG, z / _DEG)


def upsert(entity_id: str, x: float, z: float, *, kind: str = "",
           name: str = "", role: str = "") -> bool:
    """Place/move one entity (agent, NPC, building) on the city map."""
    r = _redis()
    if r is None or not entity_id:
        return False
    lon, lat = _to_lonlat(x, z)
    try:
        r.execute_command("GEOADD", _city(), lon, lat, entity_id)
        r.hset(_meta(), entity_id,
               json.dumps({"kind": kind, "name": name or entity_id, "role": role}))
        return True
    except Exception as exc:
        log.warning("geo upsert failed: %s", exc)
        return False


def sync(entities: list[dict]) -> int:
    """Bulk-place many entities in one call. Each: {id, x, z, kind?, name?, role?}.
    Call this once a second from the game loop — cheap, pipelined."""
    r = _redis()
    if r is None or not entities:
        return 0
    try:
        pipe = r.pipeline(transaction=False)
        for e in entities:
            lon, lat = _to_lonlat(e["x"], e["z"])
            pipe.execute_command("GEOADD", _city(), lon, lat, e["id"])
            pipe.hset(_meta(), e["id"], json.dumps(
                {"kind": e.get("kind", ""), "name": e.get("name", e["id"]),
                 "role": e.get("role", "")}))
        pipe.execute()
        return len(entities)
    except Exception as exc:
        log.warning("geo sync failed: %s", exc)
        return 0


def remove(entity_id: str) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.zrem(_city(), entity_id)   # GEO is a sorted set under the hood
        r.hdel(_meta(), entity_id)
    except Exception:
        pass


def _meta_of(r, ids: list[str]) -> dict:
    if not ids:
        return {}
    raw = r.hmget(_meta(), ids)
    out = {}
    for i, blob in zip(ids, raw):
        try:
            out[i] = json.loads(blob) if blob else {}
        except Exception:
            out[i] = {}
    return out


def _search(r, *args) -> list:
    return r.execute_command("GEOSEARCH", _city(), *args, "WITHDIST", "ASC")


def nearby(x: float, z: float, radius: float = 50.0, *, kind: str = "",
           role: str = "", limit: int = 20) -> list[dict]:
    """Entities within `radius` game units of a point, nearest first. Optionally
    filter by kind (e.g. 'agent', 'building') or role. Each result:
    {id, name, kind, role, dist}."""
    r = _redis()
    if r is None:
        return []
    lon, lat = _to_lonlat(x, z)
    try:
        rows = _search(r, "FROMLONLAT", lon, lat, "BYRADIUS", radius, "m",
                       "COUNT", str(max(limit, limit * 4)))
    except Exception as exc:
        log.warning("geo nearby failed: %s", exc)
        return []
    return _shape(r, rows, kind, role, limit)


def near_entity(entity_id: str, radius: float = 50.0, *, kind: str = "",
                role: str = "", limit: int = 20) -> list[dict]:
    """Same as nearby() but centred on an existing entity (e.g. the CEO)."""
    r = _redis()
    if r is None:
        return []
    try:
        rows = _search(r, "FROMMEMBER", entity_id, "BYRADIUS", radius, "m",
                       "COUNT", str(max(limit, limit * 4)))
    except Exception as exc:
        log.warning("geo near_entity failed: %s", exc)
        return []
    return [m for m in _shape(r, rows, kind, role, limit) if m["id"] != entity_id]


def _shape(r, rows, kind, role, limit) -> list[dict]:
    ids = [row[0] for row in rows]
    meta = _meta_of(r, ids)
    out = []
    for eid, dist in rows:
        m = meta.get(eid, {})
        if kind and m.get("kind") != kind:
            continue
        if role and m.get("role") != role:
            continue
        out.append({"id": eid, "name": m.get("name", eid), "kind": m.get("kind", ""),
                    "role": m.get("role", ""), "dist": round(float(dist), 1)})
        if len(out) >= limit:
            break
    return out


def count() -> int:
    r = _redis()
    if r is None:
        return 0
    try:
        return int(r.zcard(_city()))
    except Exception:
        return 0


def clear() -> None:
    r = _redis()
    if r is not None:
        try:
            r.delete(_city(), _meta())
        except Exception:
            pass


def main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    if not is_configured():
        print("Geo off — set REDIS_URL.")
        return 1
    clear()
    sync([
        {"id": "bld:cafe", "x": 100, "z": 0, "kind": "building", "name": "Bean Scene Cafe"},
        {"id": "bld:office", "x": 0, "z": 0, "kind": "building", "name": "HQ"},
        {"id": "ceo", "x": 5, "z": 5, "kind": "agent", "name": "You (CEO)", "role": "CEO"},
        {"id": "eng:1", "x": 12, "z": 8, "kind": "agent", "name": "Ada", "role": "Engineer"},
        {"id": "eng:2", "x": 95, "z": 10, "kind": "agent", "name": "Linus", "role": "Engineer"},
        {"id": "sales:1", "x": 8, "z": 30, "kind": "agent", "name": "Don", "role": "Sales"},
    ])
    print(f"placed {count()} entities.")
    print("near the CEO (r=40):")
    for m in near_entity("ceo", radius=40):
        print(f"  {m['dist']:>5}m  {m['name']} ({m['role'] or m['kind']})")
    print("nearest Engineer to the cafe:")
    for m in nearby(100, 0, radius=500, role="Engineer", limit=1):
        print(f"  {m['dist']:>5}m  {m['name']}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))

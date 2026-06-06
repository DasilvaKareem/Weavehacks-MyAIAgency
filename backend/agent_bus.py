"""Fast agent-to-agent message bus, backed by Redis Streams.

Until now agents could only coordinate slowly: leave a file in the shared drive
(SQLite) or sit in a scheduled meeting. This is a direct, real-time channel — any
agent can message a teammate (by role, name, or 'all') and pick up replies from
its own durable inbox. Built on Redis Streams so messages persist until read and
survive a recipient being offline.

Addressing: a message to 'Analyst' lands in the inbox stream keyed by that token;
an agent checks the streams for its id, its name, its role, and the broadcast
channel — so 'message the Analyst' reaches whoever holds that role.

Graceful, like every other integration: no REDIS_URL (read live) → every call is
a safe no-op and the comms tools simply aren't offered.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("company.bus")

_client = None
_tried = False


def is_configured() -> bool:
    """True when a Redis URL is set (read live, like the Gemini/Weave keys)."""
    return bool(os.getenv("REDIS_URL"))


def _ns() -> str:
    return os.getenv("COMPANY_AI_BUS_NS", "companyai")


def _redis():
    """Cached Redis client, or None if unconfigured/unreachable (never fatal)."""
    global _client, _tried
    if _client is not None or _tried:
        return _client
    _tried = True
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    try:
        from redis import Redis

        _client = Redis.from_url(url, decode_responses=True,
                                 socket_timeout=5, socket_connect_timeout=5)
        _client.ping()
        log.info("Agent bus online (Redis).")
    except Exception as exc:  # bad url / unreachable / dep missing
        log.warning("Redis unavailable (%s); agent bus offline.", exc)
        _client = None
    return _client


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "-")


_BROADCAST = {"all", "team", "everyone", "company"}


def send(to: str, content: str, from_name: str = "", from_id: str = "") -> bool:
    """Deliver a message to a teammate's inbox stream (or broadcast). Returns
    True if it was enqueued, False if the bus is offline."""
    r = _redis()
    if r is None:
        return False
    dest = "all" if _norm(to) in _BROADCAST else _norm(to)
    stream = f"{_ns()}:inbox:{dest}"
    try:
        r.xadd(stream, {"from": from_name or from_id or "?", "from_id": from_id or "",
                        "to": to, "body": content})
        r.xtrim(stream, maxlen=500, approximate=True)  # bound memory
        return True
    except Exception as exc:
        log.warning("bus send failed: %s", exc)
        return False


def inbox(agent_id: str, agent_name: str = "", role: str = "", limit: int = 20) -> list:
    """New messages for this agent since it last checked (across its id/name/role
    streams + broadcast). Advances a per-agent cursor so each message is seen once.
    Own broadcasts are filtered out."""
    r = _redis()
    if r is None:
        return []
    # Listen broadly so addressing is forgiving: id, name, the full role, AND each
    # significant word of the role — so a "Market Analyst" still receives a message
    # sent to "Analyst", and a "Research Analyst" one sent to "Researcher".
    tokens = {_norm(agent_id), "all"}
    if agent_name:
        tokens.add(_norm(agent_name))
    if role:
        tokens.add(_norm(role))
        for w in role.lower().split():
            w = _norm(w)
            if len(w) >= 4:  # skip tiny words like "of"/"the"
                tokens.add(w)
    streams = {f"{_ns()}:inbox:{t}" for t in tokens if t}

    out: list[dict] = []
    for stream in streams:
        curkey = f"{_ns()}:cur:{_norm(agent_id)}:{stream}"
        try:
            last = r.get(curkey)
            entries = r.xrange(stream, min=(f"({last}" if last else "-"),
                               max="+", count=limit)
        except Exception as exc:
            log.warning("bus read failed on %s: %s", stream, exc)
            entries = []
        for eid, fields in entries:
            r.set(curkey, eid)  # advance cursor even past our own msgs
            if fields.get("from_id") and fields.get("from_id") == agent_id:
                continue  # don't deliver our own broadcast back to us
            out.append({"id": eid, "from": fields.get("from", "?"),
                        "body": fields.get("body", "")})
    out.sort(key=lambda m: m["id"])
    return out[-limit:]


# --- tiny CLI for verifying a live Redis (python -m backend.agent_bus ...) ------

def main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    if not is_configured():
        print("REDIS_URL not set — add it to .env (Redis Cloud URL).")
        return 1
    if _redis() is None:
        print("Could not connect to Redis at REDIS_URL.")
        return 1
    cmd = argv[1] if len(argv) > 1 else "ping"
    if cmd == "send" and len(argv) >= 4:
        ok = send(argv[2], " ".join(argv[4:]) or "(empty)", from_name=argv[3])
        print("sent" if ok else "failed")
    elif cmd == "inbox" and len(argv) >= 3:
        msgs = inbox(argv[2], role=argv[2])
        print("\n".join(f"- {m['from']}: {m['body']}" for m in msgs) or "(empty)")
    else:
        print("Connected ✓. Usage: send <to> <from_name> <msg...> | inbox <agent>")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))

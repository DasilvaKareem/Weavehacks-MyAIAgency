"""Agent long-term memory — the city's shared brain, in a Redis Vector Set.

Every agent forgets the moment a task ends: the next run starts cold, with no
idea what was decided, what broke, or what a teammate already tried. This is the
fix. Each durable moment (a decision, a bug, a finished task, a chat takeaway) is
embedded with Gemini and written to a per-company Redis **Vector Set** (VADD).
Before an agent acts, recall() pulls the few most semantically-relevant memories
(VSIM K-NN) and we splice them into the Gemini system prompt — so the team acts
on what it has learned, and one agent's lesson is another agent's context.

This is the DIY core of what Redis markets as "Agent Memory + Context Retriever":
fresh, relevant context, retrieved in milliseconds, improving over time.

Graceful: no REDIS_URL or no Gemini key → every call is a safe no-op and agents
run exactly as they do today.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid

from . import embeddings
from .agent_bus import _ns, _redis

log = logging.getLogger("company.memory")


def is_configured() -> bool:
    """Memory is live only when both Redis and embeddings are available."""
    from .agent_bus import is_configured as redis_on

    return redis_on() and embeddings.is_configured()


def _company() -> str:
    return os.getenv("COMPANY_AI_SLUG", "default")


def _key(company: str | None = None) -> str:
    return f"{_ns()}:vmem:{company or _company()}"


def _values(vec: list[float]) -> list[str]:
    """VADD/VSIM take the dimension then the floats, all as command args."""
    return ["VALUES", str(len(vec)), *[repr(x) for x in vec]]


def _esc(s: str) -> str:
    return (s or "").replace('"', "'")


def remember(text: str, *, agent_id: str = "", agent_name: str = "",
             role: str = "", kind: str = "note", company: str | None = None) -> str | None:
    """Embed and store one memory. Returns its id, or None if memory is off."""
    if not (text or "").strip():
        return None
    vec = embeddings.embed(text)
    if vec is None:
        return None
    r = _redis()
    if r is None:
        return None
    mid = uuid.uuid4().hex[:16]
    attr = json.dumps({
        "text": text[:1500], "agent": agent_name or agent_id or "?",
        "agent_id": agent_id, "role": role, "kind": kind, "ts": time.time(),
    })
    try:
        r.execute_command("VADD", _key(company), *_values(vec), mid, "SETATTR", attr)
        return mid
    except Exception as exc:
        log.warning("remember failed: %s", exc)
        return None


def recall(query: str, *, k: int = 5, role: str = "", kind: str = "",
           agent_id: str = "", min_score: float = 0.6,
           company: str | None = None) -> list[dict]:
    """The k most relevant memories for `query` (optionally filtered by role/kind/
    agent), each as {text, agent, role, kind, score}. Empty list if memory is off."""
    vec = embeddings.embed(query)
    if vec is None:
        return []
    r = _redis()
    if r is None:
        return []
    cmd = ["VSIM", _key(company), *_values(vec), "WITHSCORES", "COUNT", str(max(1, k))]
    # Vector-set attribute filter: a tiny boolean expression over the JSON attrs.
    clauses = []
    if role:
        clauses.append(f'.role == "{_esc(role)}"')
    if kind:
        clauses.append(f'.kind == "{_esc(kind)}"')
    if agent_id:
        clauses.append(f'.agent_id == "{_esc(agent_id)}"')
    if clauses:
        cmd += ["FILTER", " and ".join(clauses)]
    try:
        raw = r.execute_command(*cmd)
    except Exception as exc:
        log.warning("recall failed: %s", exc)
        return []
    # WITHSCORES returns {element: score} (decoded) or a flat [el, score, ...] list.
    pairs = raw.items() if isinstance(raw, dict) else zip(raw[0::2], raw[1::2])
    out: list[dict] = []
    for el, score in pairs:
        try:
            sc = float(score)
        except (TypeError, ValueError):
            continue
        if sc < min_score:
            continue
        attr = {}
        try:
            a = r.execute_command("VGETATTR", _key(company), el)
            attr = json.loads(a) if a else {}
        except Exception:
            pass
        out.append({"text": attr.get("text", ""), "agent": attr.get("agent", "?"),
                    "role": attr.get("role", ""), "kind": attr.get("kind", ""),
                    "score": round(sc, 4)})
    return out


def recall_block(query: str, *, k: int = 5, **kw) -> str:
    """recall() rendered as a prompt-ready block, or '' when nothing relevant.
    Drop this straight into an agent's system prompt."""
    mems = recall(query, k=k, **kw)
    if not mems:
        return ""
    lines = [f"- ({m['kind']}, by {m['agent']}) {m['text']}" for m in mems]
    return ("Relevant team memory (recalled from past work — use it, "
            "don't repeat it):\n" + "\n".join(lines))


def count(company: str | None = None) -> int:
    r = _redis()
    if r is None:
        return 0
    try:
        return int(r.execute_command("VCARD", _key(company)))
    except Exception:
        return 0


def clear(company: str | None = None) -> None:
    r = _redis()
    if r is not None:
        try:
            r.delete(_key(company))
        except Exception:
            pass


# --- CLI: seed/recall to verify a live Redis (python -m backend.agent_memory ...) --

def main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    if not is_configured():
        print("Memory off — need REDIS_URL and a Gemini API key.")
        return 1
    cmd = argv[1] if len(argv) > 1 else "demo"
    if cmd == "remember" and len(argv) >= 3:
        print(remember(" ".join(argv[2:]), agent_name="cli", kind="note"))
    elif cmd == "recall" and len(argv) >= 3:
        for m in recall(" ".join(argv[2:])):
            print(f"  {m['score']}  ({m['kind']}, {m['agent']}) {m['text']}")
    elif cmd == "count":
        print(f"{count()} memories")
    elif cmd == "clear":
        clear()
        print("cleared")
    else:  # self-contained demo
        seed = [
            ("We decided to pivot pricing to $99/seat enterprise.", "CEO", "decision"),
            ("The prod Postgres cluster went down at 2am; root cause was disk full.", "DevOps", "incident"),
            ("Acme Corp is our first paying customer, signed a 12-month deal.", "Sales", "milestone"),
            ("Switched the landing page hero copy to lead with 'agentic city'.", "Marketing", "note"),
        ]
        for text, role, kind in seed:
            remember(text, agent_name=role, role=role, kind=kind)
        print(f"seeded {count()} memories. Recall test:")
        for q in ("what happened to the database", "who is paying us", "how much do we charge"):
            top = recall(q, k=1)
            hit = top[0] if top else {"score": 0, "text": "(nothing)"}
            print(f"  Q: {q!r}\n     -> {hit['score']}  {hit['text']}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))

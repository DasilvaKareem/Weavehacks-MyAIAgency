"""LangChain tools that put the Redis agent bus in every agent's hands:
message a teammate in real time, and check your own inbox. Loaded only when
REDIS_URL is set (see backend/agent_bus.py); otherwise agents just don't get them.
"""
from __future__ import annotations

from . import agent_bus


def is_configured() -> bool:
    return agent_bus.is_configured()


def load_bus_tools(agent_id: str | None, agent_name: str = "", role: str = "") -> list:
    """Comms tools for one agent, or [] if the bus isn't configured."""
    if not is_configured():
        return []
    from langchain_core.tools import tool

    aid = agent_id or _norm(agent_name) or "anon"

    @tool
    def message_agent(to: str, content: str) -> str:
        """Send a direct, real-time message to a teammate. `to` can be a ROLE
        ('Analyst', 'Researcher'), a person's name, or 'all' to broadcast to the
        whole company. Use this to delegate, ask a quick question, or share a
        finding while you work — they'll see it in their inbox."""
        ok = agent_bus.send(to, content, from_name=agent_name, from_id=aid)
        return (f"Sent to {to}." if ok else
                "[messaging offline — the Redis bus isn't reachable right now]")

    @tool
    def check_inbox() -> str:
        """Check for new messages teammates have sent you (your real-time inbox).
        Check this when you start a task or need input from someone else."""
        msgs = agent_bus.inbox(aid, agent_name=agent_name, role=role)
        if not msgs:
            return "No new messages."
        return "New messages:\n" + "\n".join(f"- {m['from']}: {m['body']}" for m in msgs)

    return [message_agent, check_inbox]


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "-")

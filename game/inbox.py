"""The phone's inbox: messages that come *to* the CEO.

Two sources feed it:
  * agents — when a hired agent finishes background work (a reply lands while you
    weren't watching), and occasional unprompted status updates;
  * NPCs — the park's businesses (recruiters, vendors) reaching out.

`Inbox` is the durable-ish store the phone reads; `InboxFeeder` is the ambient
generator that drips templated agent/NPC messages so the inbox feels alive even
when you're not actively chatting. Both are raylib-free so they stay testable;
the caller passes the current time in (the game has the clock, this module
doesn't).
"""
from __future__ import annotations

import random
from dataclasses import dataclass


def short(text: str, n: int = 32) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


@dataclass
class InboxMessage:
    sender: str                  # display name shown in the list
    subject: str                 # one-line preview
    body: str                    # full text on the read screen
    kind: str                    # agent | npc | cofounder | system
    ts: float                    # game time (pr.get_time) when it arrived
    agent_id: str | None = None  # set for hired agents → enables Reply
    read: bool = False


class Inbox:
    def __init__(self) -> None:
        self._msgs: list[InboxMessage] = []

    def post(self, sender: str, body: str, *, kind: str = "agent",
             subject: str | None = None, agent_id: str | None = None,
             ts: float = 0.0) -> InboxMessage:
        msg = InboxMessage(sender=sender, subject=subject or short(body),
                           body=body, kind=kind, ts=ts, agent_id=agent_id)
        self._msgs.append(msg)
        return msg

    def messages(self) -> list[InboxMessage]:
        """Newest first (how the phone lists them)."""
        return list(reversed(self._msgs))

    def unread(self) -> int:
        return sum(1 for m in self._msgs if not m.read)

    def mark_read(self, msg: InboxMessage) -> None:
        msg.read = True


# Templated lines so the ambient feed needs no model call. Kept generic enough to
# fit any role / business; the sender's name carries the personality.
_AGENT_LINES = [
    "Wrapped up the latest task — want to take a look?",
    "Quick update: made solid progress on my work today.",
    "Hit a small blocker; could use your steer when you're free.",
    "Shipped what we discussed. Let me know if you want changes.",
    "Got an idea for the roadmap — ping me when you have a sec?",
    "Heads up: I left a draft on the company drive for review.",
]
_NPC_LINES = [
    "we've got a deal for growing teams this week — interested?",
    "spaces are opening up in the park, want a tour?",
    "partnership opportunity — got time for a coffee?",
    "new neighbors get 10% off our services. Welcome to the block!",
    "we noticed you're hiring — we can help with that.",
]


class InboxFeeder:
    """Drips ambient agent + NPC messages on independent, jittered timers."""

    def __init__(self, agent_every: float = 95.0, npc_every: float = 160.0) -> None:
        self._agent_every = agent_every
        self._npc_every = npc_every
        self._agent_t = agent_every * 0.5   # first agent ping ~halfway in
        self._npc_t = npc_every * 0.7
        self._rng = random.Random(1234)
        self._rotate = 0                     # spread updates across the roster

    def tick(self, dt: float, agents: list, npc_names: list, inbox: Inbox,
             now: float) -> None:
        self._agent_t -= dt
        self._npc_t -= dt
        if self._agent_t <= 0:
            self._agent_t = self._agent_every + self._rng.uniform(-15, 25)
            self._emit_agent(agents, inbox, now)
        if self._npc_t <= 0:
            self._npc_t = self._npc_every + self._rng.uniform(-20, 40)
            self._emit_npc(npc_names, inbox, now)

    def _emit_agent(self, agents: list, inbox: Inbox, now: float) -> None:
        usable = [a for a in agents if getattr(a, "backend_id", None)]
        if not usable:
            return
        a = usable[self._rotate % len(usable)]
        self._rotate += 1
        inbox.post(a.name, self._rng.choice(_AGENT_LINES), kind="agent",
                   agent_id=a.backend_id, ts=now)

    def _emit_npc(self, npc_names: list, inbox: Inbox, now: float) -> None:
        if not npc_names:
            return
        name = self._rng.choice(npc_names)
        inbox.post(name, f"{name} here — {self._rng.choice(_NPC_LINES)}",
                   kind="npc", ts=now)

"""Agent-to-agent meeting orchestrator — the turn-taking brain.

A meeting takes a topic and a set of hired agents and runs a moderated
discussion: a lightweight moderator picks who speaks next (or calls it), each
agent speaks *in character* (persona + role) building on the shared transcript,
and the CEO closes with a decision. Every turn is posted to the Firebase RTDB
meeting channel (`backend/meeting_store.py`), so the game, a Pipecat voice
service, or a dashboard can all watch it live by subscribing.

Persistence is hybrid: participant identities come from local SQLite
(`AgentStore`), the live transcript lives in RTDB (`MeetingStore`).

Speaker selection is a strategy:
  * "moderated"   — an LLM facilitator picks the next voice and decides when the
                    discussion has converged (most realistic).
  * "round_robin" — cycle the attendees in order (cheaper, deterministic).

This is the text brain; Pipecat later becomes the voice layer on top, reading
and writing the same RTDB channel.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from . import company
from .agents import _extract_json, _text
from .llm import get_llm
from .persona import generate as make_persona, render_prompt as render_persona
from .store import AgentStore

CHAIR = "ceo"


def _try_meeting_store():
    """Return a MeetingStore if Firebase is configured, else None (SQLite-only)."""
    try:
        from .meeting_store import MeetingStore, available
        return MeetingStore() if available() else None
    except Exception:
        return None


@dataclass
class MeetingResult:
    cid: str
    topic: str
    summary: str
    turns: int


def _name_of(sender: str, members: dict) -> str:
    if sender == CHAIR:
        return "CEO"
    row = members.get(sender)
    return row.name if row else sender


def _format_transcript(lines: list[tuple[str, str]]) -> str:
    # lines: (display_name, content)
    return "\n".join(f"{who}: {content}" for who, content in lines) or "(no messages yet)"


class MeetingOrchestrator:
    def __init__(self, store: AgentStore | None = None,
                 meetings=None, model: str | None = None) -> None:
        self.store = store or AgentStore()
        # RTDB is the live channel; None falls back to SQLite-only (still saved).
        self.meetings = meetings if meetings is not None else _try_meeting_store()
        self.llm = get_llm(model)

    def _post(self, cid: str, sender: str, name: str, content: str) -> None:
        """Persist one turn to both the durable SQLite record and the live RTDB."""
        self.store.add_meeting_message(cid, sender, name, content)
        if self.meetings is not None:
            self.meetings.post(cid, sender, content)

    # --- setup -------------------------------------------------------------

    def open_meeting(self, topic: str, agent_ids: list[str]) -> tuple[str, dict]:
        """Create the meeting (SQLite + RTDB) + post the CEO's opening."""
        members = {}
        for aid in agent_ids:
            row = self.store.get(aid)
            if row and row.status != "fired":
                members[aid] = row
        if not members:
            raise ValueError("No valid agents for this meeting")

        cid = uuid.uuid4().hex[:12]
        self.store.create_meeting_record(cid, topic, list(members))
        if self.meetings is not None:
            self.meetings.create_meeting(topic, list(members) + [CHAIR], cid=cid)

        roster = ", ".join(f"{r.name} ({r.role})" for r in members.values())
        opening = (f"Team meeting: {topic}\n"
                   f"Attendees: {roster}. Everyone weigh in from your role — "
                   f"let's land on a clear recommendation.")
        self._post(cid, CHAIR, "CEO", opening)
        return cid, members

    # --- run ---------------------------------------------------------------

    def run(self, topic: str, agent_ids: list[str], *, max_turns: int = 6,
            mode: str = "moderated", on_event=None) -> MeetingResult:
        cid, members = self.open_meeting(topic, agent_ids)
        return self.run_meeting(cid, topic, members, max_turns=max_turns,
                                mode=mode, on_event=on_event)

    def run_meeting(self, cid: str, topic: str, members: dict, *, max_turns: int = 6,
                    mode: str = "moderated", on_event=None) -> MeetingResult:
        """Drive the turns then the CEO close. Posts every turn to RTDB as it goes."""
        ids = list(members)
        roster = "\n".join(f"{aid} — {r.name} ({r.role})" for aid, r in members.items())
        transcript: list[tuple[str, str]] = [("CEO", f"Team meeting: {topic}")]

        def emit(who: str, content: str):
            transcript.append((who, content))
            if on_event:
                on_event(who, content)

        last = None
        for turn in range(max_turns):
            speaker = self._next_speaker(mode, ids, transcript, roster, topic, turn, last)
            if speaker is None:           # moderator called the meeting
                break
            row = members[speaker]
            self._presence(cid, speaker, True)
            try:
                text = self._speak(row, speaker, topic, members, transcript)
            finally:
                self._presence(cid, speaker, False)
            self._post(cid, speaker, row.name, text)
            emit(row.name, text)
            last = speaker

        summary = self._close(topic, transcript)
        self._post(cid, CHAIR, "CEO", summary)
        emit("CEO", summary)
        self.store.finish_meeting(cid, summary)
        if self.meetings is not None:
            self.meetings.close_meeting(cid)
        return MeetingResult(cid=cid, topic=topic, summary=summary, turns=len(transcript))

    def _presence(self, cid: str, agent_id: str, speaking: bool) -> None:
        if self.meetings is not None:
            self.meetings.set_presence(cid, agent_id, speaking)

    # --- strategy: who speaks next ----------------------------------------

    def _next_speaker(self, mode, ids, transcript, roster, topic, turn, last):
        if mode == "round_robin":
            return ids[turn % len(ids)]
        # moderated: ask a facilitator, fall back to round-robin on any hiccup
        prompt = (
            f"You are the facilitator of a team meeting.\nTopic: {topic}\n"
            f"Attendees (id — name (role)):\n{roster}\n\n"
            f"Transcript so far:\n{_format_transcript(transcript)}\n\n"
            "Decide who should speak next to move toward a decision, or end the "
            "meeting if a clear recommendation has emerged or it's going in circles. "
            "Prefer voices that add a new angle over ones who just spoke. "
            'Return ONLY JSON: {"next": "<attendee id>" or "DONE", "reason": "..."}'
        )
        company_ctx = company.context_for(self.store)   # steer toward company-relevant voices
        msgs = ([("system", company_ctx)] if company_ctx else []) + [("human", prompt)]
        try:
            parsed = _extract_json(_text(self.llm.invoke(msgs)))
            if isinstance(parsed, dict):
                nxt = str(parsed.get("next", "")).strip()
                if nxt.upper() == "DONE":
                    return None
                if nxt in ids:
                    return nxt
        except Exception:
            pass
        # fallback: next attendee who isn't the last speaker
        order = [a for a in ids if a != last] or ids
        return order[turn % len(order)]

    # --- one agent's turn --------------------------------------------------

    def _speak(self, row, agent_id, topic, members, transcript) -> str:
        persona = render_persona(make_persona(agent_id, row.role))
        system = f"You are {row.name}, a {row.role} at Company.AI.\n\n{persona}"
        company_ctx = company.context_for(self.store)   # the CEO's company decisions
        if company_ctx:
            system += "\n\n" + company_ctx
        others = ", ".join(r.name for aid, r in members.items() if aid != agent_id)
        human = (
            f"You're in a team meeting. Topic: {topic}\n"
            f"Other attendees: {others} (plus the CEO).\n\n"
            f"Transcript so far:\n{_format_transcript(transcript)}\n\n"
            "It's your turn. Give your take in 2-4 sentences, fully in character. "
            "Build on what others said and add your role's perspective; push toward "
            "a decision. Don't repeat points already made, and don't narrate stage "
            "directions — just speak."
        )
        return _text(self.llm.invoke([("system", system), ("human", human)])).strip()

    # --- CEO close ---------------------------------------------------------

    def _close(self, topic, transcript) -> str:
        human = (
            f"You are the CEO wrapping up a team meeting on: {topic}\n\n"
            f"Transcript:\n{_format_transcript(transcript)}\n\n"
            "In 3-4 sentences, state the decision and concrete next steps the team "
            "converged on. Be decisive."
        )
        company_ctx = company.context_for(self.store)   # keep the wrap-up on-strategy
        msgs = ([("system", company_ctx)] if company_ctx else []) + [("human", human)]
        return _text(self.llm.invoke(msgs)).strip()


# --- CLI --------------------------------------------------------------------
# Usage:
#   python -m backend.meeting                       # list agents + their ids
#   python -m backend.meeting "<topic>"             # meet with the first 3 agents
#   python -m backend.meeting "<topic>" id1 id2 …   # meet with specific agents

def main(argv: list[str]) -> int:
    import sys

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    store = AgentStore()
    agents = store.list_agents()
    if len(argv) < 2:
        if not agents:
            print("No agents hired yet. Hire some in the game or via backend.chat.")
            return 0
        print("Agents (id  name  role):")
        for a in agents:
            print(f"  {a.id}  {a.name:<16} {a.role}")
        print('\nRun:  python -m backend.meeting "Your topic" [id1 id2 ...]')
        return 0

    topic = argv[1]
    ids = argv[2:] or [a.id for a in agents[:3]]
    if len(ids) < 2:
        print("Need at least 2 agents for a meeting.")
        return 1

    def on_event(who: str, content: str) -> None:
        print(f"\n  {who}:\n    " + content.replace("\n", "\n    "))

    print(f"=== Meeting: {topic} ===")
    res = MeetingOrchestrator(store=store).run(topic, ids, max_turns=6,
                                               mode="moderated", on_event=on_event)
    print(f"\n[meeting {res.cid} — {res.turns} turns, saved to Firebase RTDB]")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))

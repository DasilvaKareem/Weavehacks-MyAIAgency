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
from .bus_tools import load_bus_tools
from .company_fs import load_fs_tools
from .llm import get_llm
from .mcp_bridge import run_tool_loop_sync
from .persona import generate as make_persona, render_prompt as render_persona
from .store import AgentStore

CHAIR = "ceo"

# Tools an agent may reach for DURING its meeting turn. Strictly read-only recall
# so a turn stays a discussion, not a side-effecting action: it can cite prior
# work (the shared drive) and see what teammates pinged it (its inbox), but not
# write, delete, or broadcast — those belong in tasks/1:1s, not the meeting room.
_MEETING_TOOL_NAMES = {"drive_read", "drive_list", "drive_search", "check_inbox"}
_MEETING_TOOL_STEPS = 4   # cap on model<->tool round-trips per turn (keeps turns snappy)


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


def _clip(text: str, n: int) -> str:
    """Collapse whitespace and cap to `n` chars so a recap line stays compact."""
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


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
            mode: str = "moderated", on_event=None, interjections=None) -> MeetingResult:
        cid, members = self.open_meeting(topic, agent_ids)
        return self.run_meeting(cid, topic, members, max_turns=max_turns,
                                mode=mode, on_event=on_event, interjections=interjections)

    def run_meeting(self, cid: str, topic: str, members: dict, *, max_turns: int = 6,
                    mode: str = "moderated", on_event=None, interjections=None) -> MeetingResult:
        """Drive the turns then the CEO close. Posts every turn to RTDB as it goes.

        `interjections` is an optional zero-arg callable returning any new lines
        the live human CEO has spoken since last checked (the Daily talk-back
        path). Drained before each turn: each line is posted as a CEO turn and
        folded into the transcript so the moderator routes a reply and agents
        respond — but it is NOT re-voiced (the human already said it aloud)."""
        ids = list(members)
        # The moderator routes on this; annotate each attendee with the prior work
        # they bring so it can favour voices who can ground the call in real
        # artifacts. Only built for moderated mode (round-robin ignores it).
        roster = (self._roster_for_moderator(members, topic)
                  if mode != "round_robin" else "")
        transcript: list[tuple[str, str]] = [("CEO", f"Team meeting: {topic}")]

        def emit(who: str, content: str):
            transcript.append((who, content))
            if on_event:
                on_event(who, content)

        def drain_ceo() -> None:
            """Fold any live CEO interjections into the call (posted + in the
            transcript, but not voiced — the human already spoke them)."""
            if not interjections:
                return
            try:
                lines = interjections() or []
            except Exception:
                lines = []
            for raw in lines:
                line = (raw or "").strip()
                if not line:
                    continue
                self._post(cid, CHAIR, "CEO", line)
                transcript.append(("CEO", line))

        last = None
        for turn in range(max_turns):
            drain_ceo()   # let the human steer before we pick the next voice
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

        drain_ceo()   # catch a final interjection before the chair wraps up
        summary = self._close(topic, transcript)
        self._post(cid, CHAIR, "CEO", summary)
        emit("CEO", summary)
        self.store.finish_meeting(cid, summary)
        if self.meetings is not None:
            self.meetings.close_meeting(cid)
        return MeetingResult(cid=cid, topic=topic, summary=summary, turns=len(transcript))

    def run_interactive_meeting(self, cid: str, topic: str, members: dict, *,
                                turns_per_round: int = 4, mode: str = "moderated",
                                get_ceo=None, stop=None, wake=None) -> MeetingResult:
        """Human-driven meeting: the CEO prompts, the team responds, repeat.

        The AI NEVER speaks as the CEO and NEVER auto-authors a decision — the
        only CEO words in the room are the ones the human actually typed. The
        call stays live until the human closes it (the `stop` event). `get_ceo`
        returns any new CEO lines typed since last drain; `wake` is set when a new
        line arrives so we respond promptly instead of polling hot."""
        ids = list(members)
        roster = (self._roster_for_moderator(members, topic)
                  if mode != "round_robin" else "")
        # The topic the human typed is their opening prompt; seed the transcript
        # with it (the panel shows it as the title) so the team has the ask.
        transcript: list[tuple[str, str]] = [("CEO", topic)]
        last_ceo = topic

        def respond() -> None:
            """One bounded round of agent turns reacting to the latest CEO line.
            The moderator may end the round early; nobody speaks as the CEO."""
            last = None
            cap = max(2, min(turns_per_round, len(ids) + 1))
            for _ in range(cap):
                speaker = self._next_speaker(mode, ids, transcript, roster,
                                             topic, len(transcript), last)
                if speaker is None:        # moderator says this round's said enough
                    break
                row = members[speaker]
                self._presence(cid, speaker, True)
                try:
                    text = self._speak(row, speaker, topic, members, transcript)
                finally:
                    self._presence(cid, speaker, False)
                self._post(cid, speaker, row.name, text)
                transcript.append((row.name, text))
                last = speaker

        respond()   # the team reacts to the opening prompt right away

        while stop is None or not stop.is_set():
            if wake is not None:
                wake.wait(timeout=0.5)
                wake.clear()
            if stop is not None and stop.is_set():
                break
            lines = [l.strip() for l in ((get_ceo() if get_ceo else []) or [])
                     if l and l.strip()]
            if not lines:
                continue
            for line in lines:        # the human's own words, attributed to them
                self._post(cid, CHAIR, "CEO", line)
                transcript.append(("CEO", line))
                last_ceo = line
            respond()

        # The saved "decision" is the human's own final prompt — never AI text.
        self.store.finish_meeting(cid, last_ceo)
        if self.meetings is not None:
            self.meetings.close_meeting(cid)
        return MeetingResult(cid=cid, topic=topic, summary=last_ceo,
                             turns=len(transcript))

    def _presence(self, cid: str, agent_id: str, speaking: bool) -> None:
        if self.meetings is not None:
            self.meetings.set_presence(cid, agent_id, speaking)

    # --- strategy: who speaks next ----------------------------------------

    def _roster_for_moderator(self, members, topic) -> str:
        """One annotated line per attendee — `id — name (role) · what they bring`
        — so the facilitator can route toward voices with relevant prior work.
        The drive/meeting scans run ONCE here (not per attendee), and the whole
        thing degrades to the plain roster if any lookup fails."""
        try:
            files = self.store.fs_list()
        except Exception:
            files = []
        try:
            meetings = self.store.list_meetings()
        except Exception:
            meetings = []
        # Which attendees authored files matching the topic's salient keywords —
        # the strongest "has relevant prior work" signal. Gathered across all
        # attendees in one pass keyed by author.
        on_topic: dict[str, set] = {}
        try:
            kws = [w.strip(".,?!:;'\"").lower() for w in topic.split() if len(w) > 3][:4]
            seen = set()
            for kw in kws:
                for f in self.store.fs_search(kw, limit=20):
                    if f.path in seen:
                        continue
                    seen.add(f.path)
                    on_topic.setdefault(f.author_id, set()).add(f.path)
        except Exception:
            pass

        lines = []
        for aid, r in members.items():
            bits = []
            rel = sorted(on_topic.get(aid, ()))[:2]
            if rel:
                bits.append("on-topic work: " + ", ".join(rel))
            nf = sum(1 for f in files if f.author_id == aid)
            if nf:
                bits.append(f"{nf} drive file(s)")
            nm = sum(1 for m in meetings
                     if m.summary and aid in (m.members or "").split(","))
            if nm:
                bits.append(f"{nm} prior meeting(s)")
            tail = f" · {'; '.join(bits)}" if bits else ""
            lines.append(f"{aid} — {r.name} ({r.role}){tail}")
        return "\n".join(lines)

    def _next_speaker(self, mode, ids, transcript, roster, topic, turn, last):
        if mode == "round_robin":
            return ids[turn % len(ids)]
        # moderated: ask a facilitator, fall back to round-robin on any hiccup
        prompt = (
            f"You are the facilitator of a team meeting.\nTopic: {topic}\n"
            f"Attendees (id — name (role) · prior work they bring):\n{roster}\n\n"
            f"Transcript so far:\n{_format_transcript(transcript)}\n\n"
            "Decide who should speak next to move toward a decision, or end the "
            "meeting if a clear recommendation has emerged or it's going in circles. "
            "Prefer a voice that adds a new angle — or one whose notes show relevant "
            "prior work (on-topic files, past meetings) — over someone who just spoke. "
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

    def _meeting_tools(self, agent_id, row) -> list:
        """The read-only recall tools this agent may call mid-turn (see
        _MEETING_TOOL_NAMES): cite the shared drive, peek at its inbox. Inbox
        tools only exist when the Redis bus is configured. Best-effort — any
        failure yields a tool-free turn rather than aborting the meeting."""
        try:
            tools = load_fs_tools(author_id=agent_id, author_name=row.name)
            tools += load_bus_tools(agent_id, row.name, row.role)  # [] without REDIS_URL
            return [t for t in tools if t.name in _MEETING_TOOL_NAMES]
        except Exception:
            return []

    def _memory_block(self, agent_id, row) -> str:
        """A compact recap of THIS agent's recent work — recent 1:1s with the
        CEO, past meetings it sat in, and files it authored on the drive — so it
        speaks with memory instead of starting every meeting amnesiac. Each
        lookup is independently guarded; an empty recap returns ""."""
        parts: list[str] = []
        try:
            chats = self.store.history(agent_id, limit=6)
            if chats:
                lines = [f"  {'CEO' if m.role == 'human' else row.name}: "
                         f"{_clip(m.content, 160)}" for m in chats]
                parts.append("Your recent 1:1 with the CEO:\n" + "\n".join(lines))
        except Exception:
            pass
        try:
            mtgs = [m for m in self.store.list_meetings()
                    if m.summary and agent_id in (m.members or "").split(",")][:3]
            if mtgs:
                lines = [f"  • {m.topic} → {_clip(m.summary, 160)}" for m in mtgs]
                parts.append("Recent meetings you were in (topic → decision):\n"
                             + "\n".join(lines))
        except Exception:
            pass
        try:
            mine = [f for f in self.store.fs_list() if f.author_id == agent_id]
            mine.sort(key=lambda f: f.updated_at, reverse=True)
            if mine:
                lines = [f"  {f.path} [{f.kind}]" for f in mine[:8]]
                parts.append("Files you've put on the company drive "
                             "(read one with drive_read):\n" + "\n".join(lines))
        except Exception:
            pass
        if not parts:
            return ""
        return ("--- YOUR RECENT WORK (draw on it; don't just recite it) ---\n"
                + "\n\n".join(parts) + "\n--- END ---")

    def _speak(self, row, agent_id, topic, members, transcript) -> str:
        persona = render_persona(make_persona(agent_id, row.role))
        system = f"You are {row.name}, a {row.role} at Company.AI.\n\n{persona}"
        company_ctx = company.context_for(self.store)   # the CEO's company decisions
        if company_ctx:
            system += "\n\n" + company_ctx
        memory = self._memory_block(agent_id, row)      # (b) recall of own recent work
        if memory:
            system += "\n\n" + memory

        # (a) read-only recall tools, so the agent can pull up a spec or check its
        # inbox mid-turn instead of guessing. Tailor the nudge to what it actually
        # has — never invite it to call a tool that isn't bound.
        tools = self._meeting_tools(agent_id, row)
        have = {t.name for t in tools}
        recall = []
        if {"drive_search", "drive_read"} & have:
            recall.append("use drive_search/drive_read to pull up a spec or past artifact")
        if "check_inbox" in have:
            recall.append("check_inbox for anything a teammate sent you")
        recall_hint = (" If you need a detail you don't have, "
                       + ", or ".join(recall) + " before you speak.") if recall else ""

        others = ", ".join(r.name for aid, r in members.items() if aid != agent_id)
        human = (
            f"You're in a team meeting. Topic: {topic}\n"
            f"Other attendees: {others} (plus the CEO).\n\n"
            f"Transcript so far:\n{_format_transcript(transcript)}\n\n"
            "It's your turn. Keep it to 1-2 punchy sentences, fully in character — "
            "this is a fast-moving room, not a monologue. Build on what others said, "
            f"add your role's angle, and push toward a decision.{recall_hint} Don't "
            "repeat points already made, no preamble or stage directions — just speak."
        )
        msgs = [("system", system), ("human", human)]
        if tools:
            return run_tool_loop_sync(self.llm, msgs, tools,
                                      max_steps=_MEETING_TOOL_STEPS).strip()
        return _text(self.llm.invoke(msgs)).strip()

    # --- CEO close ---------------------------------------------------------

    def _close(self, topic, transcript) -> str:
        human = (
            f"You are the CEO wrapping up a team meeting on: {topic}\n\n"
            f"Transcript:\n{_format_transcript(transcript)}\n\n"
            "In 2 sentences max, state the decision and the single concrete next "
            "step the team converged on. Be decisive — no recap."
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

"""Game-side bridge to the meeting orchestrator.

Starts a meeting (off the render thread), then streams turns back to the panel
via poll() — from the Firebase RTDB live channel when available, else from the
durable SQLite transcript. Same non-blocking pattern as CompanyLink, so the
render loop never stalls on the model.
"""
from __future__ import annotations

import threading

from backend.meeting import MeetingOrchestrator


class MeetingLink:
    def __init__(self, store) -> None:
        self.store = store
        self._orch = None
        self._sub = None
        self._thread = None
        self.cid = None
        self.topic = ""
        self.members = {}      # agent_id -> AgentRow
        self._seen = 0         # SQLite fallback cursor

    def start(self, topic: str, agent_ids: list[str], *, max_turns: int = 6,
              mode: str = "moderated") -> str:
        self.topic = topic
        self._orch = MeetingOrchestrator(store=self.store)
        self.cid, self.members = self._orch.open_meeting(topic, agent_ids)
        if self._orch.meetings is not None:
            try:
                self._sub = self._orch.meetings.subscribe(self.cid)
            except Exception:
                self._sub = None
        self._thread = threading.Thread(
            target=self._run, args=(topic, max_turns, mode),
            name="meeting", daemon=True,
        )
        self._thread.start()
        return self.cid

    def _run(self, topic, max_turns, mode) -> None:
        try:
            self._orch.run_meeting(self.cid, topic, self.members,
                                   max_turns=max_turns, mode=mode)
        except Exception:
            pass  # surfaced to the UI via running() going False

    def name_of(self, sender: str) -> str:
        if sender == "ceo":
            return "CEO"
        row = self.members.get(sender)
        return row.name if row else sender

    def voice_for(self, name: str) -> str:
        """The Gemini TTS voice for a speaker name (CEO gets a fixed one)."""
        from backend import tts as engine
        if name == "CEO":
            return "Charon"
        for aid, row in self.members.items():
            if row.name == name:
                return engine.voice_for(aid)
        return "Kore"

    def poll(self) -> list[tuple[str, str]]:
        """New (speaker_name, content) lines since last poll. Non-blocking."""
        out: list[tuple[str, str]] = []
        if self._sub is not None:
            for m in self._sub.poll():
                out.append((self.name_of(m.sender), m.content))
        elif self.cid:
            lines = self.store.meeting_transcript(self.cid)
            for line in lines[self._seen:]:
                out.append((line.name, line.content))
            self._seen = len(lines)
        return out

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def shutdown(self) -> None:
        if self._sub is not None:
            self._sub.close()
            self._sub = None

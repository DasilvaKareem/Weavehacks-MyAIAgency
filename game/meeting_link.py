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
        self.voice_mode = "local"  # off | local | daily
        self.room_url = ""     # set when a Daily boardroom call goes live
        self._ceo_lines: list[str] = []   # live lines the human CEO typed in
        self._ceo_lock = threading.Lock()
        self._stop = threading.Event()    # set when the CEO closes the meeting
        self._wake = threading.Event()    # set when a new CEO line is queued

    @staticmethod
    def daily_available() -> str | None:
        """None if a Daily boardroom call can start, else why it can't (dep/key)."""
        try:
            from backend.daily_meeting import _missing
            return _missing()
        except Exception as exc:
            return str(exc)

    def start(self, topic: str, agent_ids: list[str], *, max_turns: int = 6,
              mode: str = "moderated", voice_mode: str = "local") -> str:
        self.topic = topic
        self.voice_mode = voice_mode
        self.room_url = ""
        self._stop.clear()
        self._wake.clear()
        with self._ceo_lock:
            self._ceo_lines = []
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
            if self.voice_mode == "daily":
                self._run_daily(topic, max_turns, mode)
            else:
                # off / local: a human-driven meeting. The team responds to the
                # CEO's typed prompts and NOTHING is ever spoken in the CEO's
                # name — no AI facilitator, no AI-authored decision. Stays live
                # until the CEO closes the panel. The panel polls turns from
                # RTDB/SQLite and (in local mode) voices the agents.
                self._orch.run_interactive_meeting(
                    self.cid, topic, self.members,
                    turns_per_round=max_turns, mode=mode,
                    get_ceo=self._drain_ceo, stop=self._stop, wake=self._wake)
        except Exception:
            pass  # surfaced to the UI via running() going False

    def say(self, text: str) -> None:
        """Queue a live line from the human CEO. The orchestrator folds it into
        the call before the next agent turn, so the team actually responds to the
        boss instead of deciding the company's future without them."""
        text = (text or "").strip()
        if not text:
            return
        with self._ceo_lock:
            self._ceo_lines.append(text)
        self._wake.set()

    def _drain_ceo(self) -> list[str]:
        """Hand the orchestrator (on its worker thread) any CEO lines typed since
        the last turn, and clear the queue. Thread-safe."""
        with self._ceo_lock:
            out, self._ceo_lines = self._ceo_lines, []
        return out

    def _run_daily(self, topic, max_turns, mode) -> None:
        """Voice this same meeting into a live Daily room and open a browser to
        listen. Reuses our already-opened (orch, cid, members) so it's one meeting
        — the panel keeps showing the transcript by polling RTDB/SQLite as usual."""
        import asyncio
        import webbrowser
        from backend.daily_meeting import run_daily_meeting

        def _on_room(url: str) -> None:
            self.room_url = url
            try:
                webbrowser.open(url)   # auto-open this Mac's browser to listen
            except Exception:
                pass

        # A boardroom call is a conversation, not a 6-turn briefing — give the
        # back-and-forth headroom so the human has room to interject by voice.
        asyncio.run(run_daily_meeting(
            topic, max_turns=max(max_turns, 12), mode=mode, talk_back=True,
            orch=self._orch, cid=self.cid, members=self.members,
            on_room=_on_room))

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
        self._stop.set()      # tell the interactive loop to wrap up + save
        self._wake.set()      # unblock it from waiting on the next CEO line
        if self._sub is not None:
            self._sub.close()
            self._sub = None

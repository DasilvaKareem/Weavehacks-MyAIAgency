"""Run an agent meeting out loud — each agent speaks in its own Gemini voice.

The meeting brain (`backend/meeting.py`) produces turns; this speaks each one via
Gemini TTS (`backend/tts.py`) and plays it through the speakers, blocking per turn
so voices don't overlap. The reusable Pipecat building block lives in
`backend/voice_pipeline.py` (GeminiAPITTSService) for the live/duplex future; this
narrator path is the zero-fuss "hear your company meet" version.

CLI:
    python -m backend.voice_meeting                     # list agents + ids
    python -m backend.voice_meeting "<topic>"           # voiced meeting, first 3 agents
    python -m backend.voice_meeting "<topic>" id1 id2…  # specific agents
"""
from __future__ import annotations

from . import tts as engine
from .meeting import MeetingOrchestrator
from .store import AgentStore

CEO_VOICE = "Charon"          # the boss gets a fixed, recognizable voice


class Player:
    """Blocking PCM playback through the default output device (PyAudio)."""

    def __init__(self) -> None:
        import pyaudio
        self._pa = pyaudio.PyAudio()

    def play(self, pcm: bytes) -> None:
        if not pcm:
            return
        import pyaudio
        try:
            s = self._pa.open(format=pyaudio.paInt16, channels=engine.CHANNELS,
                              rate=engine.SAMPLE_RATE, output=True)
            s.write(pcm)
            s.stop_stream()
            s.close()
        except Exception:
            pass   # no output device (e.g. headless) — stay silent, don't crash

    def close(self) -> None:
        try:
            self._pa.terminate()
        except Exception:
            pass


def run_voiced_meeting(topic: str, agent_ids: list[str], *, max_turns: int = 6,
                       mode: str = "moderated", player=None, on_event=None):
    store = AgentStore()
    orch = MeetingOrchestrator(store=store)
    cid, members = orch.open_meeting(topic, agent_ids)

    # Each agent's deterministic Gemini voice; the CEO gets a fixed one.
    name_voice = {members[a].name: engine.voice_for(a) for a in members}
    name_voice["CEO"] = CEO_VOICE

    own = player if player is not None else Player()

    def voiced(name: str, content: str) -> None:
        if on_event:
            on_event(name, content)
        try:
            own.play(engine.synth_pcm(content, name_voice.get(name, "Kore")))
        except Exception:
            pass

    try:
        return orch.run_meeting(cid, topic, members, max_turns=max_turns,
                                mode=mode, on_event=voiced)
    finally:
        if player is None:
            own.close()


def main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    store = AgentStore()
    agents = store.list_agents()
    if len(argv) < 2:
        print("Agents (id  name  role):")
        for a in agents:
            print(f"  {a.id}  {a.name:<16} {a.role}")
        print('\nRun:  python -m backend.voice_meeting "Your topic" [id1 id2 ...]')
        return 0

    topic = argv[1]
    ids = argv[2:] or [a.id for a in agents[:3]]
    if len(ids) < 2:
        print("Need at least 2 agents.")
        return 1

    def show(name, content):
        print(f"\n  🔊 {name}: " + content.replace("\n", " "))

    print(f"=== Voiced meeting: {topic} ===  (speaking aloud)")
    res = run_voiced_meeting(topic, ids, on_event=show)
    print(f"\n[meeting {res.cid} — {res.turns} turns, saved to SQLite + RTDB]")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))

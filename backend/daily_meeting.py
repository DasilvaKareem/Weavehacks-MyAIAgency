"""Run an agent meeting inside a live Daily (WebRTC) room — the "room of bots".

A single Pipecat bot joins a Daily room and *voices the whole meeting*: each
agent's turn is spoken in that agent's own Gemini voice via the reusable
`GeminiAPITTSService` (backend/voice_pipeline.py), swapping voice per turn, and a
Daily app-message tags who's speaking so any client can render captions / an
active-speaker UI. The human CEO just opens the room URL in a browser to listen
live (talk-back — VAD + STT into the same pipeline — is the next layer).

The meeting *brain* is unchanged: this reuses `MeetingOrchestrator.run_meeting`
verbatim and only adds the voice/transport layer, exactly like
`backend/voice_meeting.py` does for local speakers. The brain is synchronous and
blocks on LLM calls, so it runs in a worker thread; each turn is bridged onto the
pipeline's event loop and serialized on the bot's real playback-finished signal
(`BotStoppedSpeakingFrame`) so voices never overlap.

Why one bot, not N participants: meetings are turn-based — exactly one voice at a
time — so a single transport that switches voice per turn sounds identical to a
listener while costing one room token and one pipeline instead of N. Promoting
each agent to its own Daily participant (simultaneous presence, spatial audio) is
a clean future upgrade on top of this.

Requires:
    pip install "pipecat-ai[daily]"        # the daily-python SDK
    DAILY_API_KEY=<key from daily.co>      # creates the room + bot token
    DAILY_ROOM_URL=<url>                   # optional: reuse a fixed room

CLI:
    python -m backend.daily_meeting                      # list agents + ids
    python -m backend.daily_meeting "<topic>"            # first 3 agents
    python -m backend.daily_meeting "<topic>" id1 id2 …  # specific agents
"""
from __future__ import annotations

import asyncio
import os
import threading

from . import tts as engine
from .meeting import MeetingOrchestrator
from .store import AgentStore
from .voice_pipeline import GeminiAPITTSService

CEO_VOICE = "Charon"                 # the boss gets a fixed, recognizable voice
BOT_NAME = "Company.AI Boardroom"    # how the bot shows up in the Daily room


def _missing() -> str | None:
    """Return a human-readable reason the live room can't start, or None if OK."""
    try:
        import daily  # noqa: F401  (the daily-python SDK pulled by the [daily] extra)
    except ImportError:
        return ('Daily voice meeting needs the daily extra — '
                'pip install "pipecat-ai[daily]"')
    if not (os.getenv("DAILY_API_KEY") or os.getenv("DAILY_ROOM_URL")):
        return ("Set DAILY_API_KEY (free at daily.co) so a room + bot token can be "
                "created — or DAILY_ROOM_URL to reuse a fixed room.")
    return None


def _speech_gate():
    """A BaseObserver that flips an asyncio.Event when the bot finishes speaking a
    turn (real playback end), so the orchestrator waits before the next voice."""
    from pipecat.frames.frames import BotStoppedSpeakingFrame
    from pipecat.observers.base_observer import BaseObserver

    class _Gate(BaseObserver):
        def __init__(self) -> None:
            super().__init__()
            self.done = asyncio.Event()

        async def on_push_frame(self, data) -> None:
            if isinstance(data.frame, BotStoppedSpeakingFrame):
                self.done.set()

    return _Gate()


def _ceo_listener(sink):
    """A FrameProcessor that captures the human's mic audio between VAD
    start/stop, transcribes it via Gemini ([[backend.stt]]), and hands the text
    to `sink(text)` — the talk-back path. Transcription runs off the pipeline
    loop (asyncio.to_thread) so it never stalls audio."""
    import asyncio

    from pipecat.frames.frames import (
        InputAudioRawFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
    )
    from pipecat.processors.frame_processor import FrameProcessor

    from .stt import transcribe_pcm

    class _Listener(FrameProcessor):
        def __init__(self) -> None:
            super().__init__()
            self._buf = bytearray()
            self._rate = 16_000
            self._on = False

        async def process_frame(self, frame, direction) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, VADUserStartedSpeakingFrame):
                self._on, self._buf = True, bytearray()
            elif isinstance(frame, InputAudioRawFrame) and self._on:
                self._buf += bytes(frame.audio)
                if frame.sample_rate:
                    self._rate = frame.sample_rate
            elif isinstance(frame, VADUserStoppedSpeakingFrame) and self._on:
                self._on = False
                pcm, rate, self._buf = bytes(self._buf), self._rate, bytearray()
                if pcm:
                    text = await asyncio.to_thread(transcribe_pcm, pcm, sample_rate=rate)
                    if text:
                        sink(text)
            await self.push_frame(frame, direction)

    return _Listener()


async def run_daily_meeting(topic: str, agent_ids: list[str] | None = None, *,
                            max_turns: int = 6, mode: str = "moderated",
                            talk_back: bool = True,
                            room_url: str | None = None, token: str | None = None,
                            wait_for_listener: bool = True, listener_timeout: float = 90.0,
                            on_event=None, on_room=None,
                            orch=None, cid: str | None = None,
                            members: dict | None = None) -> dict:
    """Open a Daily room and voice a full agent meeting into it. Returns a dict
    with the room_url (share it to listen) and the MeetingResult once done.

    The game passes an already-opened meeting (orch + cid + members) so a single
    `MeetingOrchestrator` backs both the in-game transcript panel (which polls
    RTDB/SQLite) and this voice transport — one meeting, one cid. `on_room(url)`
    fires the moment the room exists, so the caller can open a browser to listen.
    Called standalone (CLI), it opens its own meeting from `agent_ids`."""
    reason = _missing()
    if reason and room_url is None:
        raise RuntimeError(reason)

    import aiohttp
    from pipecat.frames.frames import (
        EndFrame, OutputTransportMessageUrgentFrame, TTSSpeakFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.transports.daily.transport import DailyParams, DailyTransport
    from pipecat.workers.runner import WorkerRunner

    async with aiohttp.ClientSession() as session:
        # Reuse Pipecat's room helper unless the caller pinned a room explicitly.
        if room_url is None:
            from pipecat.runner.daily import configure
            room_url, token = await configure(session)
        print(f"\n🎙️  Boardroom is live — join to listen:\n    {room_url}\n")
        if on_room:
            try:
                on_room(room_url)   # let the caller open a browser to listen
            except Exception:
                pass

        # Talk-back: listen to the human's mic (needs Silero VAD to mark turns).
        # If VAD isn't installed we fall back to broadcast-only rather than fail.
        listening = talk_back
        vad_proc = None
        if listening:
            try:
                from pipecat.audio.vad.silero import SileroVADAnalyzer
                from pipecat.processors.audio.vad_processor import VADProcessor
                # In Pipecat 1.3 VAD is a pipeline stage (not a transport param):
                # it turns the mic audio into start/stop-speaking frames.
                vad_proc = VADProcessor(vad_analyzer=SileroVADAnalyzer())
            except Exception:
                listening = False

        dp = dict(audio_out_enabled=True,
                  # Gemini emits 24 kHz PCM; match it so no resampling is needed.
                  audio_out_sample_rate=engine.SAMPLE_RATE)
        if listening:
            dp["audio_in_enabled"] = True
        transport = DailyTransport(room_url, token, BOT_NAME, params=DailyParams(**dp))
        tts = GeminiAPITTSService(voice="Kore")
        gate = _speech_gate()

        # The human's transcribed interjections land here; the meeting brain
        # drains them (see interjections= below) and reacts to each as a CEO turn.
        import queue as _queue
        interject_q: _queue.Queue = _queue.Queue()

        def _drain() -> list[str]:
            out: list[str] = []
            try:
                while True:
                    out.append(interject_q.get_nowait())
            except _queue.Empty:
                pass
            return out

        stages = []
        if listening:
            stages += [transport.input(), vad_proc, _ceo_listener(interject_q.put)]
        stages += [tts, transport.output()]
        worker = PipelineWorker(
            Pipeline(stages),
            params=PipelineParams(allow_interruptions=False),
            observers=[gate],
        )

        # Open the meeting up front so we know the attendees -> their voices —
        # unless the caller (the game) already opened one and handed it in.
        if orch is None or cid is None or members is None:
            orch = MeetingOrchestrator(store=AgentStore())
            cid, members = orch.open_meeting(topic, agent_ids or [])
        name_voice = {members[a].name: engine.voice_for(a) for a in members}
        name_voice["CEO"] = CEO_VOICE
        role_of = {members[a].name: members[a].role for a in members}
        role_of["CEO"] = "CEO"

        loop = asyncio.get_running_loop()

        async def _speak(name: str, content: str) -> None:
            await tts.set_voice(name_voice.get(name, "Kore"))
            # Caption / active-speaker hint for any connected client (best-effort).
            try:
                await transport.send_message(OutputTransportMessageUrgentFrame(
                    message={"type": "meeting-turn", "speaker": name,
                             "role": role_of.get(name, ""), "text": content}))
            except Exception:
                pass
            gate.done.clear()
            await worker.queue_frame(TTSSpeakFrame(content))
            # Serialize on real playback end; fall back to a length-based estimate
            # so a dropped frame can never hang the whole meeting.
            est = max(6.0, len(content.split()) * 0.55) + 3.0
            try:
                await asyncio.wait_for(gate.done.wait(), timeout=est)
            except asyncio.TimeoutError:
                pass

        def on_turn(name: str, content: str) -> None:
            if on_event:
                on_event(name, content)
            # Bridge the sync meeting thread -> the pipeline loop, and block this
            # thread until the line has actually been spoken.
            asyncio.run_coroutine_threadsafe(_speak(name, content), loop).result()

        result: dict = {}
        meeting_done = threading.Event()

        def _run_brain() -> None:
            try:
                result["meeting"] = orch.run_meeting(
                    cid, topic, members, max_turns=max_turns, mode=mode,
                    on_event=on_turn, interjections=_drain if listening else None)
            except Exception as exc:  # surface, but always release the runner
                result["error"] = exc
            finally:
                meeting_done.set()

        started = threading.Event()

        def _start_brain() -> None:
            if not started.is_set():
                started.set()
                threading.Thread(target=_run_brain, daemon=True).start()

        @transport.event_handler("on_first_participant_joined")
        async def _on_join(_transport, _participant):
            _start_brain()  # a human is listening — begin the discussion

        async def _conductor() -> None:
            # If nobody joins in time, hold the meeting anyway (e.g. recording-only),
            # unless wait_for_listener is False, in which case start immediately.
            if not wait_for_listener:
                _start_brain()
            else:
                for _ in range(int(listener_timeout * 2)):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.5)
                if not started.is_set():
                    print("(no listener joined — running the meeting anyway)")
                    _start_brain()
            # Wait for the brain to finish, let the final audio drain, then end.
            while not meeting_done.is_set():
                await asyncio.sleep(0.25)
            await asyncio.sleep(1.5)
            await worker.queue_frame(EndFrame())

        runner = WorkerRunner()
        await runner.add_workers(worker)
        await asyncio.gather(runner.run(), _conductor())

    if result.get("error"):
        raise result["error"]
    res = result.get("meeting")
    return {"room_url": room_url, "result": res,
            "summary": getattr(res, "summary", ""), "turns": getattr(res, "turns", 0)}


def main(argv: list[str]) -> int:
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
        print('\nRun:  python -m backend.daily_meeting "Your topic" [id1 id2 ...]')
        reason = _missing()
        if reason:
            print(f"\n⚠️  To go live: {reason}")
        return 0

    reason = _missing()
    if reason:
        print(f"⚠️  {reason}")
        return 1

    topic = argv[1]
    ids = argv[2:] or [a.id for a in agents[:3]]
    if len(ids) < 2:
        print("Need at least 2 agents for a meeting.")
        return 1

    def show(name, content):
        print(f"\n  🔊 {name}: " + content.replace("\n", " "))

    print(f"=== Daily voiced meeting: {topic} ===")
    out = asyncio.run(run_daily_meeting(topic, ids, on_event=show))
    print(f"\n[meeting saved — {out['turns']} turns. Room was: {out['room_url']}]")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))

"""Background voice player for the game.

Synthesises Gemini TTS and plays it on a worker thread, one utterance at a time,
so meeting turns are spoken sequentially without ever blocking the render loop.
Degrades to silent no-ops if PyAudio / an output device isn't available.
"""
from __future__ import annotations

import queue
import threading

from backend import tts as engine


class VoicePlayer:
    def __init__(self) -> None:
        self._q: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._pa = None
        self._thread = threading.Thread(target=self._run, name="voice-player", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            import pyaudio
            self._pa = pyaudio.PyAudio()
        except Exception:
            self._pa = None
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            text, voice = item
            try:
                self._play(engine.synth_pcm(text, voice))
            except Exception:
                pass

    def _play(self, pcm: bytes) -> None:
        if not pcm or self._pa is None or self._stop.is_set():
            return
        import pyaudio
        try:
            s = self._pa.open(format=pyaudio.paInt16, channels=engine.CHANNELS,
                              rate=engine.SAMPLE_RATE, output=True)
            s.write(pcm)
            s.stop_stream()
            s.close()
        except Exception:
            pass

    def enqueue(self, text: str, voice: str) -> None:
        if text and text.strip():
            self._q.put((text, voice))

    def clear(self) -> None:
        """Drop anything still queued (e.g. when the user mutes)."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def shutdown(self) -> None:
        self._stop.set()
        self._q.put(None)

"""Push-to-talk voice for the chat panel.

Hold the talk button to record from the mic; release to transcribe. Speech →
text goes through Gemini (same key as the chat backend); the agent's reply is
spoken aloud with **Gemini TTS** (`backend.tts`), so every employee keeps one
distinct voice — the same one whether they speak in chat, on the phone, or in a
meeting (voices are assigned deterministically by agent id). Everything that can
block — the transcription and synthesis network calls — runs on worker threads,
so the render loop only ever polls.

The whole module degrades to a no-op if `sounddevice` (PortAudio) isn't
installed or the mic is unavailable (capture), or if PyAudio / an output device
is missing (playback), so the game always runs. Install capture with:
  brew install portaudio && .venv/bin/pip install sounddevice pyaudio
"""
from __future__ import annotations

import concurrent.futures
import io
import os
import threading
import wave

from backend import tts as _engine

SAMPLE_RATE = 16_000   # 16 kHz mono is plenty for speech and keeps uploads small
CHANNELS = 1

try:
    import sounddevice as sd
    _HAVE_SD = True
except Exception:       # missing PortAudio, no audio backend, etc.
    _HAVE_SD = False

_TRANSCRIBE_PROMPT = (
    "Transcribe the speech in this audio verbatim. Return ONLY the words spoken, "
    "with no commentary, quotation marks, or speaker labels. If it is silent or "
    "unintelligible, return an empty string."
)


def available() -> bool:
    """True if microphone capture is usable on this machine."""
    return _HAVE_SD


# --- recording --------------------------------------------------------------

class _Recorder:
    """Streams mic audio into a buffer between start() and stop()."""

    def __init__(self) -> None:
        self._stream = None
        self._frames: list[bytes] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        if not _HAVE_SD or self._stream is not None:
            return
        self._frames = []

        def _cb(indata, _frames, _time, _status) -> None:
            # indata is a raw CFFI buffer (int16); copy out, no numpy needed.
            with self._lock:
                self._frames.append(bytes(indata))

        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16", callback=_cb
        )
        self._stream.start()

    def stop(self) -> bytes:
        """Stop and return the recording as in-memory WAV bytes (b'' if none)."""
        if self._stream is None:
            return b""
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None
        with self._lock:
            raw = b"".join(self._frames)
            self._frames = []
        return _to_wav(raw) if raw else b""


def _to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)            # int16
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


# --- transcription (Gemini) -------------------------------------------------

_client = None


def _genai_client():
    global _client
    if _client is None:
        from google import genai
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        _client = genai.Client(api_key=key)
    return _client


def transcribe(wav_bytes: bytes, model: str) -> str:
    if not wav_bytes:
        return ""
    from google.genai import types
    resp = _genai_client().models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
            _TRANSCRIBE_PROMPT,
        ],
    )
    return (resp.text or "").strip()


# --- text-to-speech (Gemini) ------------------------------------------------
# Reuse the backend TTS engine so an employee's voice is identical everywhere —
# chat, phone, and voiced meetings all key off `backend.tts.voice_for(agent id)`.
# Synthesis is a network call, so it runs on a single worker thread; playback is
# chunked PCM through PyAudio and bails the moment a newer utterance arrives.

_tts_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="voice-tts"
)
_play_lock = threading.Lock()
_play_gen = 0                                   # bumped to cancel stale utterances
_pa = None                                      # lazily-created PyAudio instance
_pa_failed = False                              # PyAudio/output device unavailable
_tts_cache: dict[tuple[str, str], bytes] = {}   # (voice, text) -> PCM, small LRU
_CACHE_MAX = 64


def list_voices() -> list[str]:
    """The Gemini voice pool — every agent is assigned a distinct one."""
    return list(_engine.GEMINI_VOICES)


def pick_voice(seed: str) -> str | None:
    """Deterministically assign one Gemini voice to an agent (stable + unique)."""
    if not seed:
        return None
    return _engine.voice_for(seed)


def speak(text: str, voice: str | None = None) -> None:
    """Speak `text` aloud in `voice` (a Gemini voice), interrupting any utterance
    already in progress. Non-blocking: synthesis + playback happen off-thread."""
    global _play_gen
    stop_speaking()
    if not text or not text.strip():
        return
    with _play_lock:
        _play_gen += 1
        gen = _play_gen
    _tts_pool.submit(_synth_and_play, text.strip(), voice or "Kore", gen)


def _synth_and_play(text: str, voice: str, gen: int) -> None:
    if gen != _play_gen:            # superseded before we even started
        return
    key = (voice, text)
    pcm = _tts_cache.get(key)
    if pcm is None:
        try:
            pcm = _engine.synth_pcm(text, voice)
        except Exception:
            return                 # no key / offline / API error — stay silent
        if pcm:
            _tts_cache[key] = pcm
            if len(_tts_cache) > _CACHE_MAX:
                _tts_cache.pop(next(iter(_tts_cache)))
    _play_pcm(pcm, gen)


def _play_pcm(pcm: bytes, gen: int) -> None:
    global _pa, _pa_failed
    if not pcm or _pa_failed or gen != _play_gen:
        return
    try:
        import pyaudio
    except Exception:
        _pa_failed = True
        return
    if _pa is None:
        try:
            _pa = pyaudio.PyAudio()
        except Exception:
            _pa_failed = True
            return
    try:
        stream = _pa.open(format=pyaudio.paInt16, channels=_engine.CHANNELS,
                          rate=_engine.SAMPLE_RATE, output=True)
    except Exception:
        return
    chunk = _engine.SAMPLE_RATE * 2 // 5         # ~0.1s of s16 mono per write
    try:
        for i in range(0, len(pcm), chunk):
            if gen != _play_gen:                 # a newer utterance interrupted us
                break
            stream.write(pcm[i:i + chunk])
    except Exception:
        pass
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass


def stop_speaking() -> None:
    """Cancel any utterance currently synthesizing or playing."""
    global _play_gen
    with _play_lock:
        _play_gen += 1


# --- push-to-talk controller ------------------------------------------------

class VoiceInput:
    """Hold-to-talk lifecycle: begin() on press, end() on release, poll() for the
    transcript. Transcription runs on a worker thread so the panel never blocks."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._rec = _Recorder()
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="voice-stt"
        )
        self._future: concurrent.futures.Future | None = None
        self.recording = False

    def begin(self) -> None:
        if not available() or self.recording or self._future is not None:
            return
        try:
            self._rec.start()
            self.recording = True
        except Exception:
            self.recording = False   # mic denied/unavailable — stay silent

    def end(self) -> None:
        if not self.recording:
            return
        self.recording = False
        wav = self._rec.stop()
        if wav:
            self._future = self._pool.submit(transcribe, wav, self.model)

    @property
    def transcribing(self) -> bool:
        return self._future is not None

    def poll(self) -> str | None:
        """Return the transcript once ready (or '[voice error: …]'), else None."""
        if self._future is None or not self._future.done():
            return None
        fut, self._future = self._future, None
        try:
            return fut.result()
        except Exception as exc:
            return f"[voice error: {exc}]"

    def cancel(self) -> None:
        if self.recording:
            try:
                self._rec.stop()
            except Exception:
                pass
            self.recording = False
        self._future = None

    def shutdown(self) -> None:
        self.cancel()
        stop_speaking()
        self._pool.shutdown(wait=False, cancel_futures=True)

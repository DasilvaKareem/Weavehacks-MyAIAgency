"""Push-to-talk voice for the chat panel.

Hold the talk button to record from the mic; release to transcribe. Speech →
text goes through Gemini (same key as the chat backend); the agent's reply is
spoken aloud with the built-in macOS `say` command. Everything that can block —
the transcription network call — runs on a worker thread, so the render loop
only ever polls.

The whole module degrades to a no-op if `sounddevice` (PortAudio) isn't
installed or the mic is unavailable, so the game always runs. Install capture
with:  brew install portaudio && .venv/bin/pip install sounddevice
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import io
import os
import re
import subprocess
import threading
import wave

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


# --- text-to-speech (macOS `say`) -------------------------------------------

_say_proc: subprocess.Popen | None = None


_voices_cache: list[str] | None = None

# macOS ships joke/instrument "voices" that don't sound human — never assign these.
_NOVELTY = {
    "Albert", "Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos",
    "Good News", "Jester", "Organ", "Pipe Organ", "Superstar", "Trinoids",
    "Whisper", "Wobble", "Zarvox",
}


def list_voices() -> list[str]:
    """Human-sounding English macOS `say` voice names, queried once and cached."""
    global _voices_cache
    if _voices_cache is None:
        try:
            out = subprocess.run(["say", "-v", "?"], capture_output=True,
                                 text=True, timeout=5).stdout
        except Exception:
            out = ""
        names = []
        for line in out.splitlines():
            m = re.match(r"^(.+?)\s{2,}([a-z]{2})[_-][A-Z]{2}", line)
            if m and m.group(2) == "en":   # English voices read our replies best
                name = m.group(1).strip()
                if name not in _NOVELTY:
                    names.append(name)
        _voices_cache = names
    return _voices_cache


def pick_voice(seed: str) -> str | None:
    """Deterministically assign one installed voice to an agent (by id)."""
    voices = list_voices()
    if not voices:
        return None
    h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16)
    return voices[h % len(voices)]


def speak(text: str, voice: str | None = None) -> None:
    """Speak `text` aloud, interrupting any utterance already in progress."""
    global _say_proc
    stop_speaking()
    if not text:
        return
    cmd = ["say"]
    if voice:
        cmd += ["-v", voice]
    cmd.append(text)
    try:
        _say_proc = subprocess.Popen(cmd)   # non-blocking
    except Exception:
        _say_proc = None


def stop_speaking() -> None:
    global _say_proc
    if _say_proc is not None and _say_proc.poll() is None:
        _say_proc.terminate()
    _say_proc = None


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

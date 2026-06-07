"""Gemini text-to-speech engine (uses the same GEMINI_API_KEY as everything else).

Google Cloud TTS needs the Text-to-Speech API enabled on the GCP project; Gemini's
own TTS (`gemini-2.5-flash-preview-tts`) works straight off the AI Studio API key,
so this is the zero-setup path. ~30 prebuilt voices give every agent a distinct
sound; voices are assigned deterministically by agent id (stable + unique).

Returns raw PCM (16-bit, 24 kHz, mono) — what Gemini emits and what both PyAudio
and Pipecat's audio frames expect.
"""
from __future__ import annotations

import hashlib
import io
import os
import wave

MODEL = os.getenv("COMPANY_AI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
SAMPLE_RATE = 24_000
CHANNELS = 1

# The full set of Gemini prebuilt TTS voices (30) — the wider the pool, the
# longer distinct employees go before any two share a voice. Assigned by id hash.
GEMINI_VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda",
    "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus", "Iapetus",
    "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
    "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima",
    "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]

_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY not set")
        _client = genai.Client(api_key=key)
    return _client


def voice_for(seed: str) -> str:
    """Deterministically assign one Gemini voice to an agent (by id)."""
    h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16)
    return GEMINI_VOICES[h % len(GEMINI_VOICES)]


def synth_pcm(text: str, voice: str = "Kore", model: str | None = None) -> bytes:
    """Synthesize `text` to raw PCM (s16le, 24 kHz, mono)."""
    from google.genai import types

    resp = _get_client().models.generate_content(
        model=model or MODEL,
        contents=text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
        ),
    )
    return resp.candidates[0].content.parts[0].inline_data.data


def pcm_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()

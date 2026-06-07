"""Speech-to-text via Gemini — turn raw PCM into text on the AI Studio API key.

The mirror of `backend/tts.py`: where that synthesizes speech, this transcribes
it. Same zero-setup path (Gemini multimodal on `GEMINI_API_KEY`, no Cloud Speech
or Deepgram account), same client. Used by the Daily talk-back pipeline to turn
the CEO's spoken interjections into text the meeting brain can react to — and
`game/voice.py` already does the equivalent for in-game push-to-talk.
"""
from __future__ import annotations

from . import tts as engine
from .config import GEMINI_MODEL

_PROMPT = (
    "Transcribe the speech in this audio verbatim. Return ONLY the words spoken, "
    "with no commentary, quotation marks, or speaker labels. If it is silent or "
    "unintelligible, return an empty string."
)


def transcribe_pcm(pcm: bytes, *, sample_rate: int = engine.SAMPLE_RATE,
                   model: str | None = None) -> str:
    """Transcribe raw PCM (s16le, mono) to text. Empty string on silence/failure."""
    if not pcm:
        return ""
    from google.genai import types

    wav = engine.pcm_to_wav(pcm, sample_rate=sample_rate)
    try:
        resp = engine._get_client().models.generate_content(
            model=model or GEMINI_MODEL,
            contents=[types.Part.from_bytes(data=wav, mime_type="audio/wav"), _PROMPT],
        )
        return (resp.text or "").strip()
    except Exception:
        return ""   # a dropped interjection is better than a crashed meeting

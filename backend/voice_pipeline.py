"""Pipecat voice layer.

`GeminiAPITTSService` is a Pipecat `TTSService` backed by Gemini's API-key TTS.
Pipecat ships a `GeminiTTSService`, but it goes through Vertex and needs a GCP
service account with the Text-to-Speech API enabled; this one uses the AI Studio
API key the rest of the app already has, so it works with zero extra setup.

Drop it into any Pipecat pipeline:

    pipeline = Pipeline([..., GeminiAPITTSService(voice="Kore"), transport.output()])

This is the building block for live, duplex voice (a Daily room of agent bots, or
a Gemini-Live conversation with one agent) — the next step on top of it.
"""
from __future__ import annotations

import asyncio

from pipecat.frames.frames import TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame
from pipecat.services.tts_service import TTSService

from . import tts as engine


class GeminiAPITTSService(TTSService):
    def __init__(self, *, voice: str = "Kore", model: str | None = None, **kwargs) -> None:
        super().__init__(sample_rate=engine.SAMPLE_RATE, **kwargs)
        self._voice = voice
        self._model = model or engine.MODEL

    def can_generate_metrics(self) -> bool:
        return False

    async def set_voice(self, voice: str) -> None:
        self._voice = voice

    async def run_tts(self, text: str, context_id: str | None = None):
        """Synthesize one chunk of text into Pipecat audio frames."""
        yield TTSStartedFrame()
        try:
            pcm = await asyncio.to_thread(engine.synth_pcm, text, self._voice, self._model)
            yield TTSAudioRawFrame(
                audio=pcm, sample_rate=engine.SAMPLE_RATE,
                num_channels=engine.CHANNELS, context_id=context_id,
            )
        except Exception as exc:  # never let one bad utterance kill the pipeline
            from loguru import logger
            logger.error(f"GeminiAPITTSService failed: {exc}")
        yield TTSStoppedFrame()

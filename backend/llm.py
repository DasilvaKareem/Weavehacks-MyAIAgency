"""Cached Gemini client factory.

One shared client is reused across all agents — creating a client per worker
would be wasteful at scale. The client itself is safe to call concurrently;
concurrency is bounded by the semaphore in agents.py, not by client count.
"""
from __future__ import annotations

import os
from functools import lru_cache

from . import config


def _require_api_key() -> str:
    for name in config.API_KEY_ENVS:
        key = os.getenv(name)
        if key:
            return key
    primary = config.API_KEY_ENVS[0]
    raise RuntimeError(
        f"No API key set. Add one to your environment or a .env file, e.g.\n"
        f"    echo '{primary}=your_key' > .env\n"
        f"(accepted: {', '.join(config.API_KEY_ENVS)})"
    )


@lru_cache(maxsize=4)
def get_llm(model: str | None = None, temperature: float | None = None):
    """Return a cached ChatGoogleGenerativeAI client.

    Lazy import keeps `langchain_google_genai` (Python >=3.10) out of the import
    path until a model call is actually made.
    """
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError as exc:  # pragma: no cover - dependency hint
        raise RuntimeError(
            "langchain-google-genai is not installed. Install backend deps:\n"
            "    pip install langgraph langchain-google-genai python-dotenv"
        ) from exc

    return ChatGoogleGenerativeAI(
        model=model or config.GEMINI_MODEL,
        temperature=config.GEMINI_TEMPERATURE if temperature is None else temperature,
        timeout=config.REQUEST_TIMEOUT_S,
        max_retries=config.MAX_RETRIES,
        api_key=_require_api_key(),
    )

"""Gemini text embeddings — the bridge that lets Redis think in vectors.

Agent memory (backend/agent_memory.py) and the semantic Gemini cache
(backend/semantic_cache.py) both turn text into a vector here, then hand it to
a Redis Vector Set for nearest-neighbour recall. One small, cached embedder is
shared across the whole company, exactly like the chat client in llm.py.

Graceful, like every other integration: no API key (or the dep missing) → embed
returns None and the caller silently skips the vector path. Nothing breaks.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

from . import config

log = logging.getLogger("company.embed")

# gemini-embedding-001 natively returns 3072 dims; we truncate (Matryoshka) to a
# leaner 768 for faster, lighter Redis vector sets. Kept as constants so memory and
# cache size their sets identically. Sub-3072 outputs aren't unit-length, so embed()
# L2-normalizes — that makes Redis' cosine similarity clean (dot product of units).
EMBED_MODEL = os.getenv("COMPANY_AI_EMBED_MODEL", "models/gemini-embedding-001")
EMBED_DIM = int(os.getenv("COMPANY_AI_EMBED_DIM", "768"))


def _api_key() -> str | None:
    for name in config.API_KEY_ENVS:
        key = os.getenv(name)
        if key:
            return key
    return None


@lru_cache(maxsize=1)
def _embedder():
    """Cached embedder, or None when keyless/unavailable (never fatal)."""
    if not _api_key():
        return None
    try:
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        return GoogleGenerativeAIEmbeddings(
            model=EMBED_MODEL, google_api_key=_api_key(),
            output_dimensionality=EMBED_DIM)
    except Exception as exc:  # dep missing / bad key / import error
        log.warning("embeddings unavailable (%s); vector features off.", exc)
        return None


def is_configured() -> bool:
    return bool(_api_key())


def embed(text: str) -> list[float] | None:
    """Embed one piece of text, or None if embeddings are off / the call fails."""
    emb = _embedder()
    if emb is None or not (text or "").strip():
        return None
    try:
        v = emb.embed_query(text[:8000])  # cap to keep token use bounded
    except Exception as exc:
        log.warning("embed failed: %s", exc)
        return None
    # L2-normalize so Redis cosine similarity == dot product of unit vectors.
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v] if norm else v


def main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    if not is_configured():
        print("No Gemini API key set (GOOGLE_API_KEY / GEMINI_API_KEY).")
        return 1
    v = embed(" ".join(argv[1:]) or "hello world")
    if v is None:
        print("embed returned None (dep missing?)")
        return 1
    print(f"ok ✓ dim={len(v)} model={EMBED_MODEL} head={[round(x, 4) for x in v[:5]]}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))

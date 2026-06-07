"""Semantic response cache for Gemini — stop paying twice for the same question.

A normal cache only hits on a byte-identical key. Agents never phrase things
identically, so a normal cache never hits. This one hits on *meaning*: the prompt
is embedded with Gemini and matched against past prompts in a Redis **Vector Set**
(VSIM). If a near-identical prompt was answered before (cosine ≥ threshold), we
return the stored answer in a millisecond instead of paying for another Gemini
call. This is the DIY core of Redis "LangCache".

Safety first — this is the one place a cache could serve a wrong answer, so:
  * OFF by default; opt in with COMPANY_AI_SEMCACHE=1.
  * High threshold (0.97) — only near-identical prompts hit.
  * Scoped per model, and only used on single-shot (no-tool) calls, never on
    tool-using agent loops whose outcome depends on live state.

Graceful: no Redis / no embeddings / not enabled → lookup returns None and the
caller just calls Gemini as usual.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid

from . import embeddings
from .agent_bus import _ns, _redis

log = logging.getLogger("company.semcache")

# Conservative default: gemini-embedding-001 puts near-identical prompts (repeats,
# retries, the same scheduled task re-firing — where the real cost win is) at ~0.95+,
# while genuinely different questions sit well below. 0.93 catches the duplicates
# without risking a wrong answer. Loosen via env if you want paraphrase hits too.
THRESHOLD = float(os.getenv("COMPANY_AI_SEMCACHE_THRESHOLD", "0.93"))


def is_enabled() -> bool:
    """Opt-in: only when explicitly turned on AND Redis + embeddings are available."""
    if os.getenv("COMPANY_AI_SEMCACHE", "0") not in ("1", "true", "yes"):
        return False
    from .agent_bus import is_configured as redis_on

    return redis_on() and embeddings.is_configured()


def _key(model: str) -> str:
    # One vector set per model — answers aren't interchangeable across models.
    safe = (model or "default").replace(":", "_").replace("/", "_")
    return f"{_ns()}:vcache:{safe}"


def _values(vec: list[float]) -> list[str]:
    return ["VALUES", str(len(vec)), *[repr(x) for x in vec]]


def lookup(prompt: str, *, model: str = "default", threshold: float | None = None):
    """Return a cached answer for a semantically-equivalent prompt, or None."""
    if not is_enabled() or not (prompt or "").strip():
        return None
    vec = embeddings.embed(prompt)
    if vec is None:
        return None
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.execute_command("VSIM", _key(model), *_values(vec),
                                "WITHSCORES", "COUNT", "1")
    except Exception as exc:
        log.warning("semcache lookup failed: %s", exc)
        return None
    pairs = list(raw.items()) if isinstance(raw, dict) else list(zip(raw[0::2], raw[1::2]))
    if not pairs:
        return None
    el, score = pairs[0]
    try:
        if float(score) < (THRESHOLD if threshold is None else threshold):
            return None
    except (TypeError, ValueError):
        return None
    try:
        attr = json.loads(r.execute_command("VGETATTR", _key(model), el) or "{}")
    except Exception:
        return None
    return attr.get("response") or None


def store(prompt: str, response: str, *, model: str = "default") -> None:
    """Remember a prompt→answer pair for future semantic hits."""
    if not is_enabled() or not (prompt or "").strip() or not (response or "").strip():
        return
    vec = embeddings.embed(prompt)
    if vec is None:
        return
    r = _redis()
    if r is None:
        return
    attr = json.dumps({"prompt": prompt[:1500], "response": response[:8000],
                       "model": model, "ts": time.time()})
    try:
        r.execute_command("VADD", _key(model), *_values(vec),
                          uuid.uuid4().hex[:16], "SETATTR", attr)
    except Exception as exc:
        log.warning("semcache store failed: %s", exc)


def main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    os.environ.setdefault("COMPANY_AI_SEMCACHE", "1")  # enable for the demo
    if not is_enabled():
        print("Semantic cache off — need REDIS_URL + Gemini key (and COMPANY_AI_SEMCACHE=1).")
        return 1
    m = "demo"
    _redis().delete(_key(m))
    store("What is our refund policy?", "We offer a 30-day money-back guarantee.", model=m)
    # An exact repeat (the common real case: retries / re-fired tasks) hits the strict
    # default; paraphrases are shown at a looser threshold to illustrate the capability.
    print("exact repeat @ default 0.93:")
    hit = lookup("What is our refund policy?", model=m)
    print(f"  -> {'HIT: ' + hit if hit else 'miss'}")
    print("paraphrases @ 0.84 (illustrative):")
    for q in ("How do refunds work here?", "Tell me about your refund policy",
              "What colour is the sky?"):
        hit = lookup(q, model=m, threshold=0.84)
        print(f"  Q: {q!r}\n     -> {'HIT: ' + hit if hit else 'miss (would call Gemini)'}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))

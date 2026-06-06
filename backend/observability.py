"""W&B Weave tracing — turned on by a single idempotent init.

Calling init_weave() once per process is all the instrumentation we need: Weave
auto-patches LangChain/LangGraph Runnables, so every Gemini call made through
get_llm() (backend/llm.py) is traced with no per-call changes. Those traces are
also what the Observability Engineer agent reads back via backend/weave_tools.py.

Like mcp_bridge / daytona_tools / composio_tools, this is NEVER fatal: with no
WANDB_API_KEY it's a no-op and the game runs untraced.
"""
from __future__ import annotations

import logging
import os

from . import config

log = logging.getLogger("company.weave")

_inited = False
_client = None


def tag(**attrs):
    """Context manager that stamps Weave attributes onto calls made inside it.

    These attributes (role, agent_id, run_id, …) attach to the current op call
    and propagate to nested LLM calls, so the Observability Engineer can group
    cost / latency / errors PER AGENT instead of just company-wide. No-op when
    weave is absent, so it's safe to wrap hot paths.
    """
    try:
        import weave

        return weave.attributes({k: v for k, v in attrs.items() if v is not None})
    except Exception:
        from contextlib import nullcontext

        return nullcontext()


def traced(fn):
    """Wrap a function as a Weave op for a nicer nested trace tree.

    Identity when weave isn't installed, so it's safe to decorate hot paths. The
    op records calls only once init_weave() has run; decorating at import time
    (before init) is the standard Weave pattern. Works for sync and async fns.
    """
    try:
        import weave

        return weave.op(fn)
    except Exception:  # weave absent / API drift — never break the decorated fn
        return fn


def is_configured() -> bool:
    """True when a W&B API key is present (tracing + the Weave agent can go live).

    Read live from the environment (like llm.py's Gemini key), so a .env loaded
    after this module is imported still enables it.
    """
    return bool(os.getenv("WANDB_API_KEY"))


def init_weave():
    """Start Weave tracing once; return the WeaveClient (or None if unconfigured).

    Safe to call from every entrypoint and from weave_tools — repeat calls reuse
    the cached client and never re-init.
    """
    global _inited, _client
    if _inited or not is_configured():
        return _client
    try:
        import weave

        project = os.getenv("WEAVE_PROJECT") or config.WEAVE_PROJECT
        _client = weave.init(project)  # auto-patches LangChain/Gemini
        _inited = True
        log.info("Weave tracing on -> project %s", project)
    except Exception as exc:  # missing dep, bad key, network — degrade silently
        log.warning("Weave unavailable (%s); running untraced.", exc)
        _inited = True  # don't retry a broken init on every call
    return _client

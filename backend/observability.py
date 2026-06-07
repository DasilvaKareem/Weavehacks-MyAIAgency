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
        try:  # turn on live server-side monitoring of agent turns (best-effort)
            from . import weave_monitor

            weave_monitor.ensure_monitors()
        except Exception as exc:  # monitor optional — never block tracing
            log.debug("monitor bootstrap skipped: %s", exc)
    except Exception as exc:  # missing dep, bad key, network — degrade silently
        log.warning("Weave unavailable (%s); running untraced.", exc)
        _inited = True  # don't retry a broken init on every call
    return _client


# --- live (online) scoring + human feedback ---------------------------------
#
# The read side (weave_tools / weave_metrics) answers "who is expensive?". This
# is the QUALITY side: score each live reply the instant it's produced, and let
# the CEO 👍/👎 it. Both write onto the very Weave call the reply was traced as,
# so quality lands right next to cost in the same trace.

def call_op(op, *args, should_raise: bool = True, **kwargs):
    """Invoke a @traced op, returning (result, weave_call_or_None).

    With weave on, `op` is a weave op exposing `.call()` which returns
    (output, Call) — we surface the Call so the caller can score it / attach
    feedback. With weave off (`traced` was identity), there's no `.call`, so we
    just run the function and return a None call. should_raise=True preserves the
    original behavior where a hard failure propagates (chat.py salvages partials).
    """
    caller = getattr(op, "call", None)
    if caller is None:  # weave absent — op is the plain function
        return op(*args, **kwargs), None
    out, call = caller(*args, __should_raise=should_raise, **kwargs)
    return out, call


def score_call(call, scorers=None, background: bool = True) -> None:
    """Apply online scorers to a live call (logs their scores onto that call).

    Defaults to the chat-reply scorers (reply quality + coworker tone). Runs in a
    daemon thread so an interactive turn never waits on the judge. No-op when the
    call is None (weave off) or scoring isn't configured.
    """
    if call is None:
        return
    try:
        from . import weave_scorers
    except Exception:
        return
    scs = scorers if scorers is not None else weave_scorers.online_scorers()
    if not scs:
        return

    def _run():
        import asyncio

        for s in scs:
            try:
                asyncio.run(call.apply_scorer(s))  # apply_scorer is async
            except Exception as exc:  # judge/network hiccup — drop this score only
                log.debug("apply_scorer(%s) failed: %s", getattr(s, "name", s), exc)

    if background:
        import threading

        threading.Thread(target=_run, name="weave-score", daemon=True).start()
    else:
        _run()


def react(call, emoji: str = "👍", creator: str = "CEO") -> bool:
    """Attach a human 👍/👎 reaction to a traced call. Returns True on success.

    This is the CEO's feedback signal — it lands as Weave feedback on the exact
    reply call, so the People Analytics Lead can rank agents by how the CEO
    actually rates them, not just by cost. Safe no-op when call is None.
    """
    if call is None:
        return False
    try:
        call.feedback.add_reaction(emoji, creator=creator)
        return True
    except Exception as exc:
        log.debug("reaction failed: %s", exc)
        return False


def annotate(call, note: str, creator: str = "CEO") -> bool:
    """Attach a free-text note (the CEO's words) to a traced call."""
    if call is None:
        return False
    try:
        call.feedback.add_note(note, creator=creator)
        return True
    except Exception as exc:
        log.debug("note failed: %s", exc)
        return False

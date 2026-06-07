"""Live production monitors — auto-score the agents' real traces as they happen.

This registers a Weave **ClassifierMonitor** (the newest online-monitoring
primitive) over the company's live agent ops (chat_attempt / agent_attempt). Once
active, W&B scores a sample of every real agent turn server-side — so the CEO
gets a continuously-updated quality/on-task signal on PRODUCTION traffic, not
just the offline benchmark in backend/weave_evals.py.

Two layers, on purpose:
  • The server-side ClassifierMonitor (here) is the hands-off "always watching"
    dashboard view — an LLM-judge W&B runs for you on sampled calls.
  • A client-side scorer (backend/observability.score_call) runs our own Gemini
    judge on each reply via call.apply_scorer — guaranteed to work with the keys
    we already have, and what backend/chat.py uses inline.

Idempotent and never fatal: ensure_monitors() is safe to call from init_weave()
on every process; without WANDB_API_KEY it's a no-op, and any registration error
is logged and swallowed so a monitor hiccup never takes down the game.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("company.weave.monitor")

# Ops whose calls represent one agent "turn" worth grading.
_AGENT_OPS = ["chat_attempt", "agent_attempt", "autonomous_attempt"]

_MONITOR_NAME = "live-agent-quality"

# The judge prompt W&B runs server-side on each sampled turn. Kept tight and
# JSON-shaped so the classifier produces clean, chartable labels.
_JUDGE_PROMPT = (
    "You are continuously monitoring an AI company's employees. Grade this single "
    "agent turn (its output is the reply/work it produced).\n"
    "Return JSON with exactly these keys:\n"
    '  "quality": integer 0-100 (how useful/correct the work is),\n'
    '  "on_task": boolean (did it actually address what was asked),\n'
    '  "label": one of "great" | "ok" | "weak" | "failed".'
)

_done = False  # process-level guard so we only register once


def _judge_scorer():
    """A server-side LLM-judge scorer for the monitor.

    Model is configurable because online scoring runs on W&B's inference, which
    may offer a different model menu than our Gemini app key.
    """
    from weave.scorers import LLMAsAJudgeScorer
    from weave.scorers.llm_as_a_judge_scorer import LLMStructuredCompletionModel

    model_id = os.getenv("WEAVE_MONITOR_MODEL", "gpt-4o-mini")
    return LLMAsAJudgeScorer(
        name="agent_quality",
        model=LLMStructuredCompletionModel(llm_model_id=model_id),
        scoring_prompt=_JUDGE_PROMPT,
    )


def ensure_monitors():
    """Register + activate the live agent-quality monitor once. Returns its ref
    URI, or None when unconfigured or on any (swallowed) registration failure."""
    global _done
    if _done:
        return None
    from .observability import is_configured, init_weave

    if not is_configured():
        return None
    client = init_weave()
    if client is None:
        return None
    _done = True  # even on failure: don't retry a broken registration every call
    try:
        from weave.flow.monitor import ClassifierMonitor

        monitor = ClassifierMonitor(
            name=_MONITOR_NAME,
            description=(
                "Continuously LLM-judges a sample of every live agent turn "
                "(chat + company-run work) for quality and on-task-ness."
            ),
            scorers=[_judge_scorer()],
            op_names=_AGENT_OPS,
            sampling_rate=float(os.getenv("WEAVE_MONITOR_SAMPLE", "1")),
        )
        ref = monitor.activate()
        uri = ref.uri() if ref else None
        log.info("live monitor active -> %s", uri)
        return uri
    except Exception as exc:  # no inference access / API drift — keep the game up
        log.warning("live monitor not registered (%s); client-side scoring still "
                    "runs in chat.", exc)
        return None

"""Gemini-backed Weave Scorers — the quality half of the company's telemetry.

These are real `weave.Scorer` objects (not ad-hoc functions), so they plug into
BOTH offline benchmarks (backend/weave_evals.py runs them inside a
weave.Evaluation) AND live production calls (backend/chat.py applies them to each
reply via call.apply_scorer, and backend/weave_monitor.py registers them on a
Monitor). One definition, scored everywhere — that's what turns the project's
cost-only story ("who is expensive") into a cost+quality one ("who is GOOD").

Each scorer grades with the company's own Gemini model (backend/llm.py) at
temperature 0, so there's no second provider to configure and the judge is
consistent. Every scorer is defensive: a judge failure returns a neutral score
rather than raising, so scoring never breaks a chat turn or an eval row.

Without WANDB_API_KEY nothing here runs in anger — weave_evals/weave_monitor are
the gated callers; importing this module is always safe.
"""
from __future__ import annotations

import logging
import re

import weave

log = logging.getLogger("company.weave.scorers")


def _grade(prompt: str, default: int = 50) -> int:
    """Ask Gemini for a single 0-100 integer. Neutral (50) on any failure."""
    try:
        from .agents import _text
        from .llm import get_llm

        resp = get_llm(temperature=0).invoke(prompt)
        m = re.search(r"\d{1,3}", _text(resp) or "")
        return max(0, min(100, int(m.group()))) if m else default
    except Exception as exc:  # judge model down / drift — never block the caller
        log.debug("scorer grade failed: %s", exc)
        return default


_TASK_PROMPT = (
    "You are a strict, consistent hiring manager grading one AI employee's work.\n\n"
    "The task they were given:\n{task}\n\n"
    "What they produced:\n{output}\n\n"
    "Score 0-100 for how COMPLETELY and USEFULLY this does the task "
    "(0 = useless/off-topic, 100 = excellent and complete). "
    "Reply with ONLY an integer 0-100."
)

_REPLY_PROMPT = (
    "You are grading one Slack-style reply from an AI coworker to the CEO. A great "
    "reply is correct, concrete, and actually answers — natural and concise, not a "
    "corporate memo or empty filler.\n\n"
    "Their reply:\n{output}\n\n"
    "Score 0-100 for overall quality (0 = evasive/empty/wrong, 100 = sharp and "
    "genuinely helpful). Reply with ONLY an integer 0-100."
)

_TONE_PROMPT = (
    "Rate how much this AI coworker's reply sounds like a real human teammate over "
    "Slack — casual, first-person, concise — versus a stiff AI assistant or "
    "corporate memo.\n\n"
    "Reply:\n{output}\n\n"
    "0 = robotic/corporate, 100 = perfectly natural coworker. Reply with ONLY an "
    "integer 0-100."
)


class TaskQualityScorer(weave.Scorer):
    """Grades a role's benchmark answer against its task (offline evals)."""

    name: str = "task_quality"
    description: str = "0-100 LLM-judge of how well the output completes the task."

    @weave.op
    def score(self, task: str, output: str) -> dict:
        q = _grade(_TASK_PROMPT.format(task=task, output=(output or "")[:4000]))
        return {"quality": q, "passed": q >= 60}


class ReplyQualityScorer(weave.Scorer):
    """Grades a single live chat reply on its own merits (online scoring)."""

    name: str = "reply_quality"
    description: str = "0-100 LLM-judge of a live agent reply's helpfulness."

    @weave.op
    def score(self, output: str) -> dict:
        q = _grade(_REPLY_PROMPT.format(output=(output or "")[:4000]))
        return {"quality": q, "helpful": q >= 60}


class ToneScorer(weave.Scorer):
    """Grades how human/coworker-like a reply sounds (product-specific guardrail)."""

    name: str = "coworker_tone"
    description: str = "0-100 of how natural/human (vs robotic) the reply sounds."

    @weave.op
    def score(self, output: str) -> dict:
        t = _grade(_TONE_PROMPT.format(output=(output or "")[:2000]))
        return {"tone": t, "natural": t >= 60}


def eval_scorers() -> list:
    """Scorers for offline role benchmarks (need the task + output)."""
    return [TaskQualityScorer()]


def online_scorers() -> list:
    """Scorers for live chat replies (need only the output)."""
    return [ReplyQualityScorer(), ToneScorer()]

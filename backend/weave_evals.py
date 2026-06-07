"""Offline benchmarks + a published Leaderboard — the flagship Weave story.

This is where the AI workforce gets *graded*, not just metered. Every role is
wrapped as a `weave.Model` and run against a shared cross-functional benchmark
(BENCHMARK) inside a `weave.Evaluation` with the Gemini-backed scorers from
backend/weave_scorers.py. Because all roles run on the SAME Evaluation object,
Weave can rank them head-to-head — `build_leaderboard()` publishes a real
`weave.flow.leaderboard.Leaderboard` you can open in the W&B UI.

Run it (the demo):
    python -m backend.weave_evals            # eval every role + publish leaderboard
    python -m backend.weave_evals Engineer Researcher   # just these roles

Gated like everything Weave: needs WANDB_API_KEY (to log) and a Gemini key (to
answer + judge). Degrades to a clear message instead of crashing when unset.
"""
from __future__ import annotations

import asyncio
import logging
import sys

import weave

from . import config
from .observability import init_weave
from . import weave_scorers as scorers

log = logging.getLogger("company.weave.evals")

# Roles we benchmark. Canonical titles that map onto config.ROLE_PROFILES so the
# eval exercises each specialist's real system prompt.
ROLES = ["Engineer", "Researcher", "Designer", "Analyst", "Marketer",
         "DevOps", "Sales", "Recruiter"]

# A shared, cross-functional benchmark: the same business problems put to every
# role, so the leaderboard is an apples-to-apples ranking. Each row is one task;
# scorers judge how well a role's answer actually completes it.
BENCHMARK = [
    {"task": "A SaaS startup's signup conversion dropped 30% last week. List the "
             "top 3 things you'd investigate first and why."},
    {"task": "Write a crisp 2-sentence value proposition for an AI notetaker that "
             "competes with Otter and Notion."},
    {"task": "We have $5k/mo of cloud spend and it's growing 15% MoM. Give 3 "
             "concrete levers to cut it without hurting reliability."},
    {"task": "Draft a 5-step plan to land our first 10 paying B2B customers in 30 "
             "days with no ad budget."},
    {"task": "Our app stores user emails and payment info. Name the 3 most "
             "important security controls to ship before launch."},
    {"task": "Summarize why a 3-person team should (or shouldn't) adopt a "
             "monorepo. Give a one-line recommendation."},
]

_PROMPT = (
    "You are a {role} at an AI startup called Company.AI, reporting to the CEO.\n"
    "{profile}\n"
    "Do the task below with the judgment and depth your role is known for. Be "
    "concrete and useful; no filler. Keep it under 150 words.\n\n"
    "Task: {task}"
)


class RoleAgent(weave.Model):
    """One specialist role as an evaluable Weave Model.

    Kept pure-LLM (no Daytona/MCP tools) so benchmarks are fast and reliable —
    we're grading the role's *judgment*, and the live tools are already measured
    by the production monitors. The role's real system-prompt profile is injected
    so each Model genuinely behaves like that specialist.
    """

    role: str

    @weave.op
    def predict(self, task: str) -> str:
        from .agents import _text
        from .llm import get_llm

        profile = config.role_profile(self.role) or ""
        prompt = _PROMPT.format(role=self.role, profile=profile, task=task)
        try:
            return _text(get_llm(temperature=0.4).invoke(prompt)).strip()
        except Exception as exc:
            return f"[error: {exc}]"


def _evaluation() -> weave.Evaluation:
    """The one shared Evaluation every role is scored on (enables the leaderboard)."""
    ds = weave.Dataset(name="company-benchmark", rows=BENCHMARK)
    return weave.Evaluation(
        name="company-benchmark-eval",
        dataset=ds,
        scorers=scorers.eval_scorers(),
    )


async def _eval_role(evaluation: weave.Evaluation, role: str) -> dict:
    model = RoleAgent(role=role, name=f"{role}-agent")
    summary = await evaluation.evaluate(model)
    return {"role": role, "summary": summary}


def run_all(roles: list[str] | None = None) -> dict:
    """Benchmark each role on the shared Evaluation, then publish a leaderboard.

    Returns {role: mean_quality} plus the leaderboard ref, or an {'error': ...}
    dict when Weave/Gemini isn't configured (never raises for the CLI).
    """
    client = init_weave()
    if client is None:
        return {"error": "Weave not configured — set WANDB_API_KEY (and a Gemini key)."}

    roles = roles or ROLES
    evaluation = _evaluation()
    ref = weave.publish(evaluation)  # stable ref so the leaderboard can target it
    log.info("published evaluation %s", ref.uri())

    results: dict[str, float] = {}
    for role in roles:
        out = asyncio.run(_eval_role(evaluation, role))
        q = _mean_quality(out["summary"])
        results[role] = q
        print(f"  {role:<12} quality {q:5.1f}/100")

    lb_ref = build_leaderboard(ref.uri())
    return {"scores": results, "evaluation": ref.uri(), "leaderboard": lb_ref}


def _mean_quality(summary: dict) -> float:
    """Pull the mean task-quality out of an eval summary, tolerating SDK shape drift."""
    def _walk(d):
        if isinstance(d, dict):
            if "mean" in d and isinstance(d["mean"], (int, float)):
                yield d["mean"]
            for v in d.values():
                yield from _walk(v)
    # Prefer the quality field specifically; fall back to any mean we can find.
    try:
        node = summary.get("task_quality") or summary.get("TaskQualityScorer") or {}
        q = node.get("quality", {})
        if isinstance(q, dict) and isinstance(q.get("mean"), (int, float)):
            return round(float(q["mean"]), 1)
    except Exception:
        pass
    means = list(_walk(summary or {}))
    return round(sum(means) / len(means), 1) if means else 0.0


def build_leaderboard(evaluation_uri: str) -> str | None:
    """Publish a Weave Leaderboard ranking every benchmarked role by quality.

    Best-effort: returns the leaderboard ref URI, or None if publishing fails
    (so a metric-path mismatch never sinks a successful eval run).
    """
    try:
        from weave.flow.leaderboard import Leaderboard, LeaderboardColumn

        lb = Leaderboard(
            name="ai-workforce",
            description=(
                "Company.AI's AI employees ranked head-to-head on the shared "
                "cross-functional benchmark. Higher task-quality = better hire."
            ),
            columns=[
                LeaderboardColumn(
                    evaluation_object_ref=evaluation_uri,
                    scorer_name="task_quality",
                    summary_metric_path="quality.mean",
                ),
            ],
        )
        ref = weave.publish(lb)
        print(f"\n📊 Leaderboard published: {ref.uri()}")
        return ref.uri()
    except Exception as exc:
        log.warning("leaderboard publish failed (%s) — evals still logged.", exc)
        return None


def main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    roles = [a for a in argv[1:] if not a.startswith("-")] or None
    print("Benchmarking the AI workforce on Weave…\n")
    res = run_all(roles)
    if "error" in res:
        print(res["error"])
        return 1
    print("\n" + "=" * 50)
    print("  AI WORKFORCE LEADERBOARD (mean task quality)")
    print("=" * 50)
    for role, q in sorted(res["scores"].items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {role:<12} {q:5.1f}/100")
    print("=" * 50)
    if res.get("leaderboard"):
        print(f"  Open in Weave: {res['leaderboard']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

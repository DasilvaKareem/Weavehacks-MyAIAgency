"""Weave-backed tools: let the Observability Engineer agent read the company's
own LLM traces.

This is the read side of the loop. backend/observability.py turns ON tracing
(every Gemini/LangChain call is logged to W&B Weave); here the Observability
Engineer queries those same traces — cost, token usage, latency, errors, and
per-agent economics — so it can report on (and optimize) the AI workforce
instead of guessing. The heavy analytics live in backend/weave_metrics.py.

Mirrors daytona_tools.py: SDK-backed @tools, gated by a key, returning [] when
unconfigured and "[weave error: ...]" on any runtime failure (never fatal).
"""
from __future__ import annotations

import logging

from . import weave_metrics as wm

log = logging.getLogger("company.weave")


def is_configured() -> bool:
    """True when Weave tracing is configured (so there are traces to read)."""
    from .observability import is_configured as _cfg

    return _cfg()


def load_weave_tools(author_id: str | None = None, author_name: str = "") -> list:
    """Return the Weave trace-reading tools, or [] if tracing isn't configured."""
    if not is_configured():
        return []
    from .observability import init_weave

    client = init_weave()
    if client is None:  # dep missing / bad key — degrade to prompt-only
        return []

    from langchain_core.tools import tool

    @tool
    def llm_spend_report(limit: int = 300) -> str:
        """Summarize recent LLM token usage and dollar cost across all agents.

        Use this to answer "how much are we spending on the AI team?".
        """
        try:
            calls = wm.fetch_calls(client, limit)
            llm = [c for c in calls if wm.is_llm_call(c)]
            if not llm:
                return ("No traces found yet — run the company once, or check "
                        "WANDB_API_KEY is set so calls are traced.")
            pin = pout = 0
            dollars = 0.0
            for c in llm:
                a, b = wm.call_tokens(c)
                pin += a
                pout += b
                dollars += wm.call_cost(c)
            total = pin + pout
            per_k = wm.fmt_usd(dollars / total * 1000) if total else "$0.00"
            cpg = wm.cost_per_goal(calls)
            return (
                f"Across the last {len(llm)} LLM call(s) (of {len(calls)} ops):\n"
                f"- tokens: {total:,} ({pin:,} in / {pout:,} out)\n"
                f"- cost: {wm.fmt_usd(dollars)} (~{per_k}/1k tokens)\n"
                f"- cost per company run (goal): {wm.fmt_usd(cpg)}"
            )
        except Exception as exc:
            return f"[weave error: {exc}]"

    @tool
    def agent_economics(limit: int = 400) -> str:
        """Cost, latency, tokens and crash rate per agent from live Weave traces.

        Shows each HIRED employee's own track record when available (from their
        chats/jobs), and the per-role view from company runs. Use it for any
        question about who is expensive, slow, or failing.
        """
        try:
            calls = wm.fetch_calls(client, limit)
            people = wm.agent_breakdown(calls)
            parts = []
            if people:
                parts.append(wm.render_agents(people))
            parts.append(wm.render_breakdown(wm.role_breakdown(calls)))
            return "\n\n".join(parts)
        except Exception as exc:
            return f"[weave error: {exc}]"

    @tool
    def optimization_verdict(limit: int = 400) -> str:
        """Identify the weakest-link agent and recommend a concrete fix.

        Grounded in real Weave numbers (cost/call, latency, error rate vs the
        company median). Use this to decide who to coach, re-model, or fire.
        """
        try:
            calls = wm.fetch_calls(client, limit)
            return wm.render_verdict(wm.optimization_verdict(calls))
        except Exception as exc:
            return f"[weave error: {exc}]"

    @tool
    def apply_optimization(limit: int = 400) -> str:
        """Act on the verdict: cut the weakest agent's budget so the NEXT run is cheaper.

        Reads live traces, finds the weakest-link role, and records a per-role
        override the workers obey. Use this when the CEO says "optimize the
        company" or "make us cheaper". The next company run will reflect it.
        """
        try:
            from . import optimizer
            calls = wm.fetch_calls(client, limit)
            verdict = wm.optimization_verdict(calls)
            if not verdict:
                return "Not enough trace data yet — run the company once, then optimize."
            change = optimizer.apply_action(verdict)
            if not change.get("role"):
                return "Nothing to optimize — all agents look healthy."
            return (f"Optimized {change['role']}: {change['change']}. "
                    f"Reason: {verdict.get('reason','')}. The next run will be cheaper.")
        except Exception as exc:
            return f"[weave error: {exc}]"

    @tool
    def optimization_policy() -> str:
        """Show the per-role overrides currently in effect (and how to reset)."""
        try:
            from . import role_policy
            pol = role_policy.all()
            if not pol:
                return "No overrides active — every role is on its default model/budget."
            lines = ["Active per-role overrides (from prior optimizations):"]
            for role, o in pol.items():
                lines.append(f"- {role}: {o}")
            return "\n".join(lines)
        except Exception as exc:
            return f"[weave error: {exc}]"

    @tool
    def recent_failures(limit: int = 25) -> str:
        """List the most recent traced calls that errored. Use for reliability questions."""
        try:
            calls = wm.fetch_calls(client, max(limit * 4, 80))
            failed = [c for c in calls if getattr(c, "exception", None)]
            if not failed:
                return f"No errors in the last {len(calls)} traced call(s). ✅"
            lines = [f"{len(failed)} recent failure(s):"]
            for c in failed[:limit]:
                msg = str(getattr(c, "exception", "")).strip().splitlines()
                lines.append(f"- {wm.op_short(c)}: {(msg[0] if msg else '?')[:200]}")
            return "\n".join(lines)
        except Exception as exc:
            return f"[weave error: {exc}]"

    return [llm_spend_report, agent_economics, optimization_verdict,
            apply_optimization, optimization_policy, recent_failures]

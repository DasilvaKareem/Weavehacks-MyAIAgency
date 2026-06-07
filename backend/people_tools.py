"""People-analytics tools — the HR role that grades the AI workforce on quality.

Where the Observability Engineer (backend/weave_tools.py) reads COST, the People
Analytics Lead reads QUALITY: it runs the offline benchmark (weave.Evaluation +
published Leaderboard), reads each employee's live online scores and the CEO's
👍/👎, and ranks the team. This is the read side of the Evaluate → Monitor →
Leaderboard → Feedback loop the project is built around.

Same contract as weave_tools/daytona_tools: SDK-backed @tools gated by a key,
returning a clear message (never raising) when Weave or Gemini isn't configured.
"""
from __future__ import annotations

import logging

from . import weave_metrics as wm

log = logging.getLogger("company.people")


def is_configured() -> bool:
    from .observability import is_configured as _cfg

    return _cfg()


def load_people_tools(author_id: str | None = None, author_name: str = "") -> list:
    """Return the People Analytics tools, or [] when Weave tracing isn't on."""
    if not is_configured():
        return []
    from .observability import init_weave

    client = init_weave()
    if client is None:
        return []

    from langchain_core.tools import tool

    @tool
    def workforce_leaderboard(limit: int = 400) -> str:
        """Rank every employee by a blended live QUALITY score (not just cost).

        Combines each agent's online reply-quality scores, the CEO's 👍/👎, and
        their crash rate from real Weave traces. Use this for "who are our best/
        worst people?", "who should get more work?", or "who's underperforming?".
        """
        try:
            calls = wm.fetch_calls(client, limit)
            return wm.render_leaderboard(wm.workforce_leaderboard(calls))
        except Exception as exc:
            return f"[people error: {exc}]"

    @tool
    def agent_report_card(who: str, limit: int = 400) -> str:
        """A single employee's full card: quality, CEO feedback, cost, latency, crashes.

        `who` can be an agent id, a name, or a role. Use it before deciding to
        promote, coach, or reassign someone.
        """
        try:
            calls = wm.fetch_calls(client, limit)
            rows = wm.workforce_leaderboard(calls)
            low = (who or "").lower().strip()
            match = [r for r in rows if low in str(r["agent_id"]).lower()
                     or low in (r["name"] or "").lower()
                     or low in (r["role"] or "").lower()]
            if not match:
                return (f"No traced quality data for {who!r} yet — have them reply "
                        "to a few messages (replies get auto-scored), then retry.")
            r = match[0]
            q = f"{r['quality']:.0f}/100" if r["quality"] is not None else "no scores yet"
            tone = f"{r['tone']:.0f}/100" if r["tone"] is not None else "—"
            return (
                f"📋 {r['name']} ({r['role']})\n"
                f"- blended score : {r['score']:.0f}/100\n"
                f"- reply quality : {q} (over {r['replies_scored']} scored replies)\n"
                f"- coworker tone : {tone}\n"
                f"- CEO feedback  : {r['thumbs_up']}👍 / {r['thumbs_down']}👎\n"
                f"- cost / call   : {wm.fmt_usd(r['cost_per_call'])}\n"
                f"- avg latency   : {r['avg_latency']:.2f}s\n"
                f"- crash rate    : {r['error_rate']*100:.0f}%"
            )
        except Exception as exc:
            return f"[people error: {exc}]"

    @tool
    def quality_pulse(limit: int = 400) -> str:
        """Company-wide quality + CEO satisfaction right now (one-paragraph pulse).

        Use for "how's the team doing?" or a standup-style health check.
        """
        try:
            calls = wm.fetch_calls(client, limit)
            rows = wm.workforce_leaderboard(calls)
            scored = [r for r in rows if r["quality"] is not None]
            if not scored:
                return ("No quality signal yet — agents need to reply to a few "
                        "messages so the online scorers can grade them.")
            avg_q = sum(r["quality"] for r in scored) / len(scored)
            up = sum(r["thumbs_up"] for r in rows)
            down = sum(r["thumbs_down"] for r in rows)
            best, worst = rows[0], rows[-1]
            return (
                f"Team quality is averaging {avg_q:.0f}/100 across "
                f"{len(scored)} graded employee(s). CEO sentiment: {up}👍/{down}👎. "
                f"Top performer: {best['name']} ({best['score']:.0f}). "
                f"Needs attention: {worst['name']} ({worst['score']:.0f})."
            )
        except Exception as exc:
            return f"[people error: {exc}]"

    @tool
    def run_benchmark(roles: str = "") -> str:
        """Run the offline role benchmark and publish the Weave Leaderboard.

        Scores each role on the shared cross-functional benchmark with LLM-judge
        scorers, then publishes a head-to-head Leaderboard to W&B. `roles` is an
        optional comma-separated subset (e.g. "Engineer, Researcher"); blank runs
        them all. This is slower (it actually runs the agents) — use it when the
        CEO asks to "benchmark" or "evaluate" the team's skills.
        """
        try:
            from . import weave_evals
            picked = [r.strip() for r in roles.split(",") if r.strip()] or None
            res = weave_evals.run_all(picked)
            if "error" in res:
                return res["error"]
            ranking = sorted(res["scores"].items(), key=lambda kv: kv[1], reverse=True)
            lines = ["📊 Benchmark complete — roles by task quality:"]
            for role, q in ranking:
                lines.append(f"- {role}: {q:.0f}/100")
            if res.get("leaderboard"):
                lines.append(f"\nLeaderboard published to Weave: {res['leaderboard']}")
            return "\n".join(lines)
        except Exception as exc:
            return f"[people error: {exc}]"

    return [workforce_leaderboard, agent_report_card, quality_pulse, run_benchmark]

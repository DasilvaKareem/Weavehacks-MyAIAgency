"""HR tools — the only role whose tools act on the company *itself*.

Every other profiled role reaches outward (Opsera, Apify, Daytona, Composio).
The Human Resources agent instead operates on the same SQLite store that backs
the whole company (backend/store.py): it reads the roster, inspects an agent's
real activity, records performance reviews, and fires people. Those writes are
live — a fired agent immediately drops out of `list_agents()`, so the office
loses them on the next poll, and an evaluation persists as the on-record score.

The store is a single local file at a fixed default path, so these tools just
open it themselves (exactly like backend/chat.py's `store or AgentStore()`),
which keeps them stateless LangChain tools with no wiring to thread through.
"""
from __future__ import annotations

from .store import AgentStore

# How many recent chat turns to surface when reviewing an agent's work. Bounds
# the token cost of a review while still giving the HR agent real signal.
_REVIEW_HISTORY = 12
_SNIPPET = 240  # per-message char cap in a review, so one long reply can't flood it


def _fmt_score(store: AgentStore, agent_id: str) -> str:
    ev = store.latest_evaluation(agent_id)
    return f"{ev.score}/100" if ev else "unrated"


def load_hr_tools() -> list:
    """LangChain tools that let an HR agent manage the company's hired agents.

    Unlike the exec layer these are always on: they're scoped to the company's
    own roster (no shell, no filesystem), so there's nothing to gate behind a
    danger flag — the worst they do is fire a teammate, which the agent is
    explicitly hired to do.
    """
    from langchain_core.tools import tool

    store = AgentStore()

    @tool
    def list_team(include_fired: bool = False) -> str:
        """List every hired agent with id, name, role, department, current status,
        hire date, and latest performance score. Call this FIRST — you need the
        agent ids it returns to review, evaluate, or fire anyone. Set
        include_fired=True to also see people already let go."""
        agents = store.list_agents(include_fired=include_fired)
        if not agents:
            return "No agents are currently hired."
        lines = [f"{len(agents)} agent(s):"]
        for a in agents:
            lines.append(
                f"  {a.id}  {a.name} — {a.role} ({a.dept or 'n/a'}) "
                f"[{a.status}]  score {_fmt_score(store, a.id)}  hired {a.hired_at}"
            )
        return "\n".join(lines)

    @tool
    def review_agent(agent_id: str) -> str:
        """Pull one agent's full record so you can judge performance: their role,
        status, hire date, recent conversation activity with the CEO, and past
        evaluations. Base any score you give on what you see here — never guess."""
        a = store.get(agent_id)
        if a is None:
            return f"No agent with id {agent_id!r}. Run list_team to see valid ids."
        out = [
            f"{a.name} — {a.role} ({a.dept or 'n/a'})",
            f"  id {a.id} | status {a.status} | hired {a.hired_at}",
        ]
        history = store.history(a.id, limit=_REVIEW_HISTORY)
        if history:
            out.append(f"  Recent activity (last {len(history)} turns):")
            for m in history:
                who = "CEO" if m.role == "human" else a.name
                text = " ".join(m.content.split())
                if len(text) > _SNIPPET:
                    text = text[:_SNIPPET] + "…"
                out.append(f"    [{who}] {text}")
        else:
            out.append("  Recent activity: none — this agent hasn't done any work yet.")
        evals = store.list_evaluations(a.id)
        if evals:
            out.append("  Evaluation history:")
            for e in evals:
                out.append(f"    {e.ts} — {e.score}/100 by {e.reviewer}: {e.summary}")
        else:
            out.append("  Evaluation history: never reviewed.")
        return "\n".join(out)

    @tool
    def evaluate_agent(agent_id: str, score: int, summary: str) -> str:
        """Record a performance review for an agent. score is 0-100 (clamped);
        summary is your written rationale. Review the agent with review_agent
        before scoring so the evaluation reflects real work."""
        a = store.get(agent_id)
        if a is None:
            return f"No agent with id {agent_id!r}. Run list_team to see valid ids."
        store.add_evaluation(a.id, score, summary, reviewer="HR")
        clamped = max(0, min(100, int(score)))
        return f"Recorded {clamped}/100 for {a.name} ({a.role}): {summary}"

    @tool
    def fire_agent(agent_id: str, reason: str) -> str:
        """Terminate an agent. This is live and immediate — they drop off the
        roster and out of the office. The reason is filed as a final 0/100
        evaluation on record. You cannot fire a fellow HR agent (including
        yourself); evaluate them instead and escalate to the CEO."""
        a = store.get(agent_id)
        if a is None:
            return f"No agent with id {agent_id!r}. Run list_team to see valid ids."
        if a.status == "fired":
            return f"{a.name} has already been let go."
        low = (a.role or "").lower()
        if "hr" in low or "human resource" in low:
            return (f"Refused: {a.name} is an HR role. HR can't fire HR — record an "
                    f"evaluation and raise it with the CEO instead.")
        store.add_evaluation(a.id, 0, f"Terminated: {reason}", reviewer="HR")
        store.fire(a.id)
        return f"Fired {a.name} ({a.role}). Reason on record: {reason}"

    @tool
    def staffing_review() -> str:
        """Recommend who to FIRE / COACH / REPURPOSE / KEEP, grounded in LIVE W&B
        Weave telemetry (cost, latency, and crash rate per role) joined to the real
        roster. Call this BEFORE firing anyone so the decision is data-driven, not a
        guess — it tells you exactly which hired agent is broken or wasteful and the
        id to act on. Then use fire_agent or repurpose_agent."""
        try:
            from .observability import init_weave, is_configured
            from . import weave_metrics as wm
            if not is_configured():
                return ("No observability data — WANDB_API_KEY isn't set, so I can't "
                        "ground staffing on telemetry. Review agents manually instead.")
            client = init_weave()
            if client is None:
                return "Weave is unavailable right now; can't pull telemetry."
            recs = wm.staffing_recommendations(
                wm.fetch_calls(client, 400), store.list_agents())
            if not recs:
                return ("No agent traces yet — run the company at least once so "
                        "agents are measured, then review.")
            lines = ["Data-driven staffing review (from live Weave traces):"]
            for r in recs:
                a = r["agent"]
                lines.append(f"- [{r['action']}] {a.name} — {a.role} "
                             f"(id {a.id}): {r['reason']}")
            lines.append("\nAct with fire_agent(id, reason) or "
                         "repurpose_agent(id, new_role, reason).")
            return "\n".join(lines)
        except Exception as exc:
            return f"[staffing review error: {exc}]"

    @tool
    def repurpose_agent(agent_id: str, new_role: str, reason: str) -> str:
        """Repurpose an agent into a new role instead of firing them — use when
        someone underperforms in their current role but could add value elsewhere
        (cheaper than re-hiring). Changes their role live and files a note."""
        a = store.get(agent_id)
        if a is None:
            return f"No agent with id {agent_id!r}. Run list_team to see valid ids."
        if a.status == "fired":
            return f"{a.name} has already been let go — can't repurpose."
        old = a.role
        store.set_role(agent_id, new_role)
        store.add_evaluation(a.id, 50, f"Repurposed {old} → {new_role}: {reason}",
                             reviewer="HR")
        return f"Repurposed {a.name}: {old} → {new_role}. Reason on record: {reason}"

    @tool
    def retune_agent(agent_id: str, reason: str) -> str:
        """Make an agent LEANER instead of firing or reassigning — caps its tool
        budget and drops it to the cheaper model so it costs less on its next
        run/chat. Use for a REPURPOSE verdict when the agent works fine but is too
        expensive. Cheaper than firing-and-rehiring, and reversible."""
        from . import role_policy, config
        a = store.get(agent_id)
        if a is None:
            return f"No agent with id {agent_id!r}. Run list_team to see valid ids."
        new_steps = max(1, config.MCP_MAX_TOOL_STEPS // 3)
        role_policy.set(a.role, max_tool_steps=new_steps, model=config.CHEAP_MODEL)
        store.add_evaluation(a.id, 60, f"Retuned for efficiency: {reason}", reviewer="HR")
        return (f"Retuned {a.name} ({a.role}): tool budget → {new_steps} steps, "
                f"model → {config.CHEAP_MODEL}. Next run will cost less. Reason: {reason}")

    @tool
    def team_report() -> str:
        """A one-shot overview of the whole company: headcount, a breakdown by
        status and department, and the average performance score. Use this when
        the CEO asks how the team is doing overall."""
        agents = store.list_agents(include_fired=True)
        active = [a for a in agents if a.status != "fired"]
        if not agents:
            return "No agents have ever been hired."
        by_status: dict[str, int] = {}
        by_dept: dict[str, int] = {}
        for a in active:
            by_status[a.status] = by_status.get(a.status, 0) + 1
            by_dept[a.dept or "n/a"] = by_dept.get(a.dept or "n/a", 0) + 1
        scores = [e.score for a in active
                  if (e := store.latest_evaluation(a.id)) is not None]
        avg = f"{sum(scores) / len(scores):.0f}/100" if scores else "n/a (no reviews)"
        fired = len(agents) - len(active)
        out = [
            f"Headcount: {len(active)} active, {fired} let go ({len(agents)} all-time).",
            "By status: " + ", ".join(f"{k} {v}" for k, v in sorted(by_status.items())),
            "By department: " + ", ".join(f"{k} {v}" for k, v in sorted(by_dept.items())),
            f"Average performance score: {avg} "
            f"({len(scores)} of {len(active)} reviewed).",
        ]
        return "\n".join(out)

    return [list_team, review_agent, evaluate_agent, staffing_review,
            repurpose_agent, retune_agent, fire_agent, team_report]

"""Analytics over Weave traces — the brain of the self-optimizing company.

This turns raw Weave traces into per-agent economics (cost, latency, tokens,
failures) and a concrete optimization verdict: which agent is the weakest link
and what to do about it. The Observability Engineer surfaces this; the CEO/HR
act on it (see backend/optimizer.py) — closing the loop where Weave telemetry
actually drives decisions, not just dashboards.

All extractors are defensive (Weave field shapes vary by SDK version) and shared
with backend/weave_tools.py.
"""
from __future__ import annotations

from statistics import median

# --- defensive extractors ---------------------------------------------------

def op_short(call) -> str:
    name = getattr(call, "display_name", None) or getattr(call, "op_name", "") or "?"
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.split(":", 1)[0]


def latency_s(call):
    start, end = getattr(call, "started_at", None), getattr(call, "ended_at", None)
    if start and end:
        try:
            return (end - start).total_seconds()
        except Exception:
            return None
    return None


def _summary(call) -> dict:
    return getattr(call, "summary", None) or {}


def costs(call) -> dict:
    return (_summary(call).get("weave") or {}).get("costs") or {}


def usage(call) -> dict:
    return _summary(call).get("usage") or {}


def attrs(call) -> dict:
    a = getattr(call, "attributes", None) or {}
    try:
        return dict(a)
    except Exception:
        return {}


def is_llm_call(call) -> bool:
    """Only the leaf model op carries un-double-counted usage (Weave rolls usage up)."""
    return "Llm." in (getattr(call, "op_name", "") or "")


def is_attempt(call) -> bool:
    """The per-task agent op (agents.agent_attempt) — one per task, raises on crash.

    This is the right denominator for crash rate: one attempt = one task, and its
    call.exception is set iff the agent actually failed.
    """
    return op_short(call) == "agent_attempt"


def call_cost(call) -> float:
    total = 0.0
    for c in costs(call).values():
        total += (c.get("prompt_tokens_total_cost", 0.0) or 0.0)
        total += (c.get("completion_tokens_total_cost", 0.0) or 0.0)
    return total


def call_tokens(call) -> tuple[int, int]:
    pin = pout = 0
    for u in usage(call).values():
        pin += u.get("prompt_tokens", 0) or 0
        pout += u.get("completion_tokens", 0) or 0
    return pin, pout


def fmt_usd(d: float) -> str:
    if d <= 0:
        return "$0.00"
    if d >= 0.01:
        return f"${d:.2f}"
    return f"${d:.6f}"


def fetch_calls(client, limit: int = 500):
    """Most-recent-first calls with costs, degrading across SDK versions."""
    sort = [{"field": "started_at", "direction": "desc"}]
    for kwargs in (
        {"limit": limit, "include_costs": True, "sort_by": sort},
        {"limit": limit, "include_costs": True},
        {"limit": limit},
    ):
        try:
            return list(client.get_calls(**kwargs))
        except TypeError:
            continue
    return list(client.get_calls())


# --- aggregations -----------------------------------------------------------

def _row(rows: dict, role: str) -> dict:
    return rows.setdefault(role, {"calls": 0, "in": 0, "out": 0, "cost": 0.0,
                                  "lat": 0.0, "timed": 0, "tasks": 0, "errors": 0})


def role_breakdown(calls) -> dict:
    """Per-role economics: cost/tokens/latency from the leaf LLM calls, and crash
    rate from the per-task agent_attempt ops (one per task, exception iff crashed).
    """
    rows: dict[str, dict] = {}
    for c in calls:
        role = attrs(c).get("role")
        if not role:
            continue
        if is_llm_call(c):  # cost / tokens / latency
            r = _row(rows, role)
            r["calls"] += 1
            pin, pout = call_tokens(c)
            r["in"] += pin
            r["out"] += pout
            r["cost"] += call_cost(c)
            lat = latency_s(c)
            if lat is not None:
                r["lat"] += lat
                r["timed"] += 1
        elif is_attempt(c):  # crash rate (one attempt = one task)
            r = _row(rows, role)
            r["tasks"] += 1
            if getattr(c, "exception", None):
                r["errors"] += 1
    for r in rows.values():
        r["tokens"] = r["in"] + r["out"]
        r["cost_per_call"] = r["cost"] / r["calls"] if r["calls"] else 0.0
        r["in_per_call"] = r["in"] / r["calls"] if r["calls"] else 0.0
        r["avg_latency"] = r["lat"] / r["timed"] if r["timed"] else 0.0
        # crash rate over real tasks; 0 when a role hasn't run a full task yet
        r["error_rate"] = r["errors"] / r["tasks"] if r["tasks"] else 0.0
    return rows


def run_breakdown(calls) -> dict:
    """Per-company-run cost, keyed by run_id (-> cost_per_goal)."""
    runs: dict[str, dict] = {}
    for c in calls:
        if not is_llm_call(c):
            continue
        rid = attrs(c).get("run_id")
        if not rid:
            continue
        run = runs.setdefault(rid, {"goal": attrs(c).get("run_goal", "?"),
                                    "cost": 0.0, "calls": 0})
        run["cost"] += call_cost(c)
        run["calls"] += 1
    return runs


def cost_per_goal(calls) -> float:
    runs = run_breakdown(calls)
    if not runs:
        return 0.0
    return sum(r["cost"] for r in runs.values()) / len(runs)


# --- the verdict ------------------------------------------------------------

def optimization_verdict(calls) -> dict:
    """Pick the weakest-link role and recommend a concrete action.

    Returns {} when there isn't enough data to judge. The recommendation is
    grounded in real Weave numbers (cost-per-call, latency, error rate vs the
    company median), so the CEO/HR action is defensible, not arbitrary.
    """
    rows = role_breakdown(calls)
    if len(rows) < 2:
        return {}
    med_cost = median([r["cost_per_call"] for r in rows.values()]) or 1e-12
    med_lat = median([r["avg_latency"] for r in rows.values()]) or 1e-12

    worst, score = None, -1.0
    for role, r in rows.items():
        # weighted badness: cost & latency relative to peers, plus failures
        s = (r["cost_per_call"] / med_cost) + (r["avg_latency"] / med_lat) + r["error_rate"] * 5
        if s > score:
            worst, score = role, s
    r = rows[worst]
    cost_x = r["cost_per_call"] / med_cost
    lat_x = r["avg_latency"] / med_lat
    # A token-heavy role is bloated by tool output cycling through the prompt —
    # capping its tool budget is the real fix, not a cheaper model.
    tool_heavy = r["in_per_call"] >= 4000

    if r["error_rate"] >= 0.34:
        action, reason = "coach_or_fire", f"failing {r['error_rate']*100:.0f}% of calls"
    elif cost_x >= 1.25 and tool_heavy:
        action, reason = ("reduce_tool_budget",
                          f"{cost_x:.1f}x median cost, {r['in_per_call']:,.0f} input "
                          f"tok/call (tool output bloating the prompt)")
    elif cost_x >= 1.25:
        action, reason = "switch_cheaper_model", f"{cost_x:.1f}x the median cost per call"
    elif lat_x >= 1.5:
        action, reason = "reduce_tool_budget", f"{lat_x:.1f}x the median latency"
    else:
        action, reason = "ok", "within normal range of peers"

    return {
        "worst_role": worst,
        "action": action,
        "reason": reason,
        "cost_per_call": r["cost_per_call"],
        "cost_x_median": cost_x,
        "latency_x_median": lat_x,
        "error_rate": r["error_rate"],
        "rows": rows,
    }


# --- text rendering (for the agent's tool replies) --------------------------

def render_breakdown(rows: dict) -> str:
    if not rows:
        return "No per-agent traces yet — run the company once so agents are traced."
    order = sorted(rows.items(), key=lambda kv: kv[1]["cost"], reverse=True)
    lines = ["Per-agent economics (from live Weave traces):"]
    for role, r in order:
        crash = (f"{r['error_rate']*100:.0f}% crash ({r['errors']}/{r['tasks']} tasks)"
                 if r["tasks"] else "no tasks yet")
        lines.append(
            f"- {role}: {r['calls']} call(s), {r['tokens']:,} tok, "
            f"{fmt_usd(r['cost'])} ({fmt_usd(r['cost_per_call'])}/call), "
            f"{r['avg_latency']:.2f}s avg, {crash}"
        )
    return "\n".join(lines)


_ACTION_TEXT = {
    "switch_cheaper_model": "Switch this role to a cheaper/faster model",
    "reduce_tool_budget": "Cut its tool-step budget (it's looping/slow)",
    "coach_or_fire": "Coach or fire this agent (HR)",
    "ok": "No action needed",
}


def render_verdict(v: dict) -> str:
    if not v:
        return ("Not enough data for a verdict yet — run the company a couple of "
                "times so at least two roles are traced.")
    if v["action"] == "ok":
        return f"All agents are within normal range. Weakest is {v['worst_role']} but it's fine."
    return (
        f"⚠️ Weakest link: {v['worst_role']} — {v['reason']}.\n"
        f"   cost/call {fmt_usd(v['cost_per_call'])} ({v['cost_x_median']:.1f}x median), "
        f"latency {v['latency_x_median']:.1f}x median, errors {v['error_rate']*100:.0f}%.\n"
        f"   Recommended action: {_ACTION_TEXT.get(v['action'], v['action'])}."
    )

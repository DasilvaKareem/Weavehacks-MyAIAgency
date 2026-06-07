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


# Canonical role buckets, so a hired "Research Analyst" joins the planner's
# "Researcher" trace. Most-specific keywords first (mirrors config._match_profile).
_ROLE_KEYWORDS = (
    "observ", "devops", "research", "data scien", "engineer", "design", "ux",
    "market", "analyst", "sales", "recruit", "human resource", "hr", "assistant",
    "document", "sheets", "support", "operations", "finance", "blog",
)


def canon_role(role: str) -> str:
    """Map any role title to a canonical bucket so traces and the roster join."""
    low = (role or "").lower()
    for kw in _ROLE_KEYWORDS:
        if kw in low:
            return kw
    return low.split()[0] if low.split() else low


def is_attempt(call) -> bool:
    """A per-task agent op that raises on crash — graph workers (agent_attempt) or
    1:1 chats (chat_attempt). One attempt = one task; call.exception set iff it
    actually failed. The right denominator for crash rate.
    """
    return op_short(call) in ("agent_attempt", "chat_attempt", "autonomous_attempt")


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
        {"limit": limit, "include_costs": True, "include_feedback": True, "sort_by": sort},
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


def agent_breakdown(calls) -> dict:
    """Per-INDIVIDUAL economics, keyed by the real hired agent_id (from chats /
    assigned tasks). This is the genuine per-person track record — cost, latency,
    and crash rate for that specific employee, not a role average.
    """
    rows: dict[str, dict] = {}
    for c in calls:
        aid = attrs(c).get("agent_id")
        if not aid:
            continue
        if is_llm_call(c):
            r = _row(rows, aid)
            r["calls"] += 1
            pin, pout = call_tokens(c)
            r["in"] += pin
            r["out"] += pout
            r["cost"] += call_cost(c)
            lat = latency_s(c)
            if lat is not None:
                r["lat"] += lat
                r["timed"] += 1
            r.setdefault("name", attrs(c).get("agent_name"))
            r.setdefault("role", attrs(c).get("role"))
        elif is_attempt(c):
            r = _row(rows, aid)
            r["tasks"] += 1
            if getattr(c, "exception", None):
                r["errors"] += 1
            r.setdefault("name", attrs(c).get("agent_name"))
            r.setdefault("role", attrs(c).get("role"))
    for r in rows.values():
        r["tokens"] = r["in"] + r["out"]
        r["cost_per_call"] = r["cost"] / r["calls"] if r["calls"] else 0.0
        r["avg_latency"] = r["lat"] / r["timed"] if r["timed"] else 0.0
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


def staffing_recommendations(calls, roster) -> list:
    """Join per-role Weave economics to the hired roster -> a per-agent verdict.

    `roster` is an iterable of objects with .id/.name/.role. Returns dicts sorted
    FIRE → COACH → REPURPOSE → KEEP so HR can act worst-first. This is the bridge
    that lets the HR agent decide who to fire/repurpose from real telemetry rather
    than guessing — the Observability Engineer measures, HR acts.
    """
    per_agent = agent_breakdown(calls)   # real, id-keyed (from this person's chats)
    role_rows = role_breakdown(calls)    # role-level fallback (from graph runs)
    if not per_agent and not role_rows:
        return []

    # role buckets for fallback when an individual has no personal traces yet
    bucket: dict[str, dict] = {}
    for role, r in role_rows.items():
        b = bucket.setdefault(canon_role(role),
                              {"cost_per_call": 0.0, "error_rate": 0.0, "tasks": 0, "n": 0})
        b["cost_per_call"] += r["cost_per_call"]
        b["error_rate"] = max(b["error_rate"], r["error_rate"])
        b["tasks"] += r["tasks"]
        b["n"] += 1
    for b in bucket.values():
        if b["n"]:
            b["cost_per_call"] /= b["n"]

    # median over whatever cost-per-call signals we actually have
    costs_seen = [r["cost_per_call"] for r in per_agent.values() if r["calls"]] or \
                 [r["cost_per_call"] for r in role_rows.values() if r["calls"]]
    med_cost = median(costs_seen) if costs_seen else 1e-12

    recs = []
    for a in roster:
        m = per_agent.get(getattr(a, "id", None))
        basis = "own traces"
        if not m or m["tasks"] == 0:           # no personal data → role proxy
            m = bucket.get(canon_role(getattr(a, "role", "")))
            basis = "role average"
        if not m or m["tasks"] == 0:
            recs.append({"agent": a, "action": "KEEP", "basis": "none",
                         "reason": "no telemetry yet (hasn't done traced work)", "metrics": m})
            continue
        cost_x = m["cost_per_call"] / med_cost
        if m["error_rate"] >= 0.5:
            action, reason = "FIRE", f"crashing {m['error_rate']*100:.0f}% of tasks"
        elif m["error_rate"] >= 0.25:
            action, reason = "COACH", f"failing {m['error_rate']*100:.0f}% of tasks"
        elif cost_x >= 1.75:
            action, reason = "REPURPOSE", f"{cost_x:.1f}x the median cost per call"
        else:
            action, reason = "KEEP", "performing within range of peers"
        recs.append({"agent": a, "action": action, "reason": f"{reason} ({basis})",
                     "metrics": m, "cost_x": cost_x, "basis": basis})

    rank = {"FIRE": 0, "COACH": 1, "REPURPOSE": 2, "KEEP": 3}
    recs.sort(key=lambda x: rank.get(x["action"], 9))
    return recs


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


def render_agents(rows: dict) -> str:
    """Per-individual economics (keyed by real agent_id) for hired employees."""
    if not rows:
        return ("No per-agent traces yet — chat with a hired agent (or run their "
                "jobs) so their work is attributed to them, then check again.")
    order = sorted(rows.items(), key=lambda kv: kv[1]["cost"], reverse=True)
    lines = ["Per-employee economics (each agent's OWN live Weave traces):"]
    for aid, r in order:
        who = r.get("name") or aid
        role = r.get("role") or "?"
        crash = (f"{r['error_rate']*100:.0f}% crash ({r['errors']}/{r['tasks']})"
                 if r["tasks"] else "no tasks yet")
        lines.append(
            f"- {who} ({role}, id {aid}): {r['calls']} call(s), "
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


# --- quality + feedback (the People Analytics layer) ------------------------
#
# Online scorers (backend/weave_scorers.py) and the CEO's 👍/👎 both land as
# Weave *feedback* on the reply call (call.summary.weave.feedback). These
# extractors read that back so an agent can be ranked by how GOOD and how LIKED
# it is — not just how cheap. Defensive: feedback shape varies by SDK version.

def _feedback_rows(call) -> list:
    """The feedback entries attached to a call (scores + reactions), or []."""
    fb = (_summary(call).get("weave") or {}).get("feedback")
    try:
        return list(fb) if fb else []
    except Exception:
        return []


def _score_value(payload: dict, *fields) -> float | None:
    """Pull a 0-100 score out of a runnable-scorer feedback payload.

    apply_scorer nests the scorer's return under 'output' (sometimes directly),
    so we look for the named numeric fields anywhere in the payload tree.
    """
    out = payload.get("output", payload) if isinstance(payload, dict) else {}
    if not isinstance(out, dict):
        return None
    for f in fields:
        v = out.get(f)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def call_quality(call) -> dict:
    """Per-call quality signal from feedback: {quality, tone, thumbs} (any may be absent)."""
    out = {"quality": None, "tone": None, "thumbs": 0}
    for row in _feedback_rows(call):
        ftype = row.get("feedback_type", "") or ""
        payload = row.get("payload", {}) or {}
        if ftype.startswith("wandb.reaction"):
            emoji = payload.get("detoned") or payload.get("emoji") or ""
            if emoji == "👍":
                out["thumbs"] += 1
            elif emoji == "👎":
                out["thumbs"] -= 1
        elif "runnable" in ftype:
            q = _score_value(payload, "quality")
            t = _score_value(payload, "tone")
            if q is not None:
                out["quality"] = q
            if t is not None:
                out["tone"] = t
    return out


def _blend(econ: dict, quality, thumbs_up, thumbs_down) -> float:
    """A single 0-100 employee score: quality, minus crashes, plus CEO love.

    Quality (the online judge) is the backbone; crashes hurt hard; each net
    thumbs-up nudges ±. Falls back to a neutral 60 when no judge score exists yet
    so a brand-new hire isn't ranked last purely for lack of data.
    """
    base = quality if quality is not None else 60.0
    base *= (1.0 - econ.get("error_rate", 0.0))     # a crashing agent isn't "good"
    base += 3 * (thumbs_up - thumbs_down)           # the CEO's thumb counts
    return max(0.0, min(100.0, base))


def workforce_leaderboard(calls) -> list:
    """Rank hired employees by a blended quality score from live Weave data.

    Joins each agent's economics (cost/latency/crash) to their online quality
    scores and the CEO's 👍/👎, returning rows sorted best-first. This is the
    People Analytics Lead's headline view and what the in-game phone shows.
    """
    econ = agent_breakdown(calls)        # cost/latency/crash, keyed by agent_id
    # accumulate quality + thumbs per agent from chat-reply feedback
    qual: dict[str, dict] = {}
    for c in calls:
        aid = attrs(c).get("agent_id")
        if not aid:
            continue
        cq = call_quality(c)
        q = qual.setdefault(aid, {"q": [], "t": [], "up": 0, "down": 0,
                                  "name": attrs(c).get("agent_name"),
                                  "role": attrs(c).get("role")})
        if cq["quality"] is not None:
            q["q"].append(cq["quality"])
        if cq["tone"] is not None:
            q["t"].append(cq["tone"])
        if cq["thumbs"] > 0:
            q["up"] += cq["thumbs"]
        elif cq["thumbs"] < 0:
            q["down"] += -cq["thumbs"]

    rows = []
    ids = set(econ) | set(qual)
    for aid in ids:
        e = econ.get(aid, {})
        qd = qual.get(aid, {})
        q_list = qd.get("q", [])
        t_list = qd.get("t", [])
        quality = sum(q_list) / len(q_list) if q_list else None
        tone = sum(t_list) / len(t_list) if t_list else None
        up, down = qd.get("up", 0), qd.get("down", 0)
        rows.append({
            "agent_id": aid,
            "name": e.get("name") or qd.get("name") or aid,
            "role": e.get("role") or qd.get("role") or "?",
            "score": _blend(e, quality, up, down),
            "quality": quality,
            "tone": tone,
            "thumbs_up": up,
            "thumbs_down": down,
            "cost_per_call": e.get("cost_per_call", 0.0),
            "avg_latency": e.get("avg_latency", 0.0),
            "error_rate": e.get("error_rate", 0.0),
            "calls": e.get("calls", 0),
            "replies_scored": len(q_list),
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def render_leaderboard(rows: list) -> str:
    """Text leaderboard for the People Analytics Lead's tool reply."""
    if not rows:
        return ("No employee quality data yet — chat with a hired agent (their "
                "replies get auto-scored), then check again.")
    lines = ["🏆 AI workforce leaderboard (live quality, from Weave):"]
    for i, r in enumerate(rows, 1):
        q = f"{r['quality']:.0f}" if r["quality"] is not None else "—"
        love = ""
        if r["thumbs_up"] or r["thumbs_down"]:
            love = f", CEO {r['thumbs_up']}👍/{r['thumbs_down']}👎"
        crash = f", {r['error_rate']*100:.0f}% crash" if r["error_rate"] else ""
        lines.append(
            f"{i}. {r['name']} ({r['role']}) — score {r['score']:.0f}/100 "
            f"[quality {q}, {fmt_usd(r['cost_per_call'])}/call{crash}{love}]"
        )
    return "\n".join(lines)

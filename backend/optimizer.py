"""The self-optimization loop: Weave telemetry -> decision -> measurable savings.

`optimize_once(goal)` runs the company, reads its OWN Weave traces to find the
weakest-link agent, applies a concrete fix (a per-role override the worker
obeys), then runs the same goal again and reports the before/after cost. The
whole point of the project in one function: the AI company watching its traces
and making itself cheaper.

CLI (the demo):
    python -m backend.optimizer "Research the top 5 note-taking apps and pitch one"
"""
from __future__ import annotations

import sys
import time

from . import config, role_policy
from . import weave_metrics as wm
from .observability import traced

_FLUSH_WAIT_S = 6  # let Weave's server catch up before we query the run back


def _flush() -> None:
    try:
        import weave

        weave.flush()
    except Exception:
        pass
    time.sleep(_FLUSH_WAIT_S)


def _latest_run_cost(client) -> tuple[float, str | None]:
    """Cost of the most recently traced company run (by run_id)."""
    calls = wm.fetch_calls(client, 300)
    rid = None
    for c in calls:
        if wm.is_llm_call(c):
            rid = wm.attrs(c).get("run_id")
            if rid:
                break
    runs = wm.run_breakdown(calls)
    return (runs.get(rid, {}).get("cost", 0.0) if rid else 0.0), rid


def apply_action(verdict: dict) -> dict:
    """Turn a verdict into a real per-role override. Returns what changed."""
    role = verdict.get("worst_role")
    action = verdict.get("action", "ok")
    if not role:
        return {"role": None, "change": "nothing to optimize"}

    # Even an "ok" verdict optimizes the top cost driver, so the loop always has
    # a lever to pull — but only with a meaningfully smaller budget than default.
    if action in ("reduce_tool_budget", "coach_or_fire", "ok"):
        new_steps = max(1, config.MCP_MAX_TOOL_STEPS // 3)
        role_policy.set(role, max_tool_steps=new_steps)
        return {"role": role, "change": f"tool budget -> {new_steps} steps "
                f"(was {config.MCP_MAX_TOOL_STEPS})"}
    if action == "switch_cheaper_model":
        cheap = config.CHEAP_MODEL
        role_policy.set(role, model=cheap)
        return {"role": role, "change": f"model -> {cheap}"}
    return {"role": role, "change": "no-op"}


_JUDGE_PROMPT = """You are grading an AI company's work — be a strict, consistent judge.

Goal the company was given:
{goal}

The company's final report:
{report}

Score 0-100 for how COMPLETELY and USEFULLY this achieves the goal (0 = useless/
off-topic, 100 = excellent and complete). Reply with ONLY an integer 0-100."""


def _parse_score(text: str) -> int:
    import re

    m = re.search(r"\d{1,3}", text or "")
    return max(0, min(100, int(m.group()))) if m else 50


@traced
def judge_run(goal: str, report: str) -> int:
    """LLM-judge a run's output 0-100. Traced, so the quality signal lives in Weave
    alongside cost — this is what stops the loop from optimizing quality away."""
    from .llm import get_llm

    try:
        resp = get_llm(temperature=0).invoke(
            _JUDGE_PROMPT.format(goal=goal, report=(report or "")[:4000]))
        from .agents import _text
        return _parse_score(_text(resp))
    except Exception:
        return 50  # neutral on judge failure — never block the loop


def optimize_loop(goal: str, rounds: int = 3, quality_tol: int = 5,
                  reset: bool = True) -> dict:
    """Iterative, quality-guarded self-improvement. Each round: measure cost AND
    quality, change the weakest agent, and KEEP the change only if quality held
    (else revert). Policy persists across rounds, so the company genuinely learns
    to run itself cheaper without getting worse. Returns the round-by-round trend.
    """
    from .observability import init_weave
    from .orchestrator import Orchestrator

    client = init_weave()
    if client is None:
        raise RuntimeError("Weave not configured — set WANDB_API_KEY to optimize.")
    if reset:
        role_policy.reset()

    orch = Orchestrator()
    trend: list[dict] = []
    try:
        report = orch.run_blocking(goal, timeout=300)
        _flush()
        cost = _latest_run_cost(client)[0]
        quality = judge_run(goal, report)
        trend.append({"round": 0, "cost": cost, "quality": quality, "change": "baseline",
                      "kept": True})
        print(f"[round 0] baseline: cost {wm.fmt_usd(cost)}, quality {quality}/100")

        for i in range(1, rounds + 1):
            verdict = wm.optimization_verdict(wm.fetch_calls(client, 400))
            if not verdict or verdict.get("action") == "ok":
                print(f"[round {i}] converged — no agent worth changing.")
                break
            role = verdict["worst_role"]
            snapshot = role_policy.get(role)            # to revert if it backfires
            change = apply_action(verdict)
            print(f"[round {i}] trying: {role} — {change['change']}")

            report = orch.run_blocking(goal, timeout=300)
            _flush()
            new_cost = _latest_run_cost(client)[0]
            new_quality = judge_run(goal, report)

            # Guardrail: keep only if it's cheaper AND quality didn't materially drop.
            kept = (new_cost <= cost) and (new_quality >= quality - quality_tol)
            if kept:
                cost, quality = new_cost, new_quality
                verdict_note = "kept ✓"
            else:
                role_policy.replace(role, snapshot)     # revert — it hurt
                verdict_note = "reverted ✗ (hurt quality or cost)"
            trend.append({"round": i, "cost": new_cost, "quality": new_quality,
                          "change": f"{role}: {change['change']}", "kept": kept})
            print(f"[round {i}] cost {wm.fmt_usd(new_cost)}, quality "
                  f"{new_quality}/100 → {verdict_note}")
            if not kept:
                break  # the best available lever backfired; we've converged
    finally:
        orch.shutdown()

    base, final = trend[0], trend[-1]
    kept_final = [t for t in trend if t["kept"]][-1]
    return {
        "goal": goal,
        "trend": trend,
        "cost_before": base["cost"],
        "cost_after": kept_final["cost"],
        "quality_before": base["quality"],
        "quality_after": kept_final["quality"],
        "pct_saved": (1 - kept_final["cost"] / base["cost"]) * 100 if base["cost"] else 0.0,
    }


def optimize_once(goal: str, reset: bool = True) -> dict:
    """Baseline run -> optimize the weakest agent -> re-run. Returns before/after."""
    from .observability import init_weave
    from .orchestrator import Orchestrator

    client = init_weave()
    if client is None:
        raise RuntimeError("Weave not configured — set WANDB_API_KEY to optimize.")
    if reset:
        role_policy.reset()  # start from the true baseline for a clean comparison

    orch = Orchestrator()
    try:
        print(f"\n[1/2] Baseline run: {goal!r}")
        orch.run_blocking(goal, timeout=300)
        _flush()
        cost_before, run_before = _latest_run_cost(client)
        calls = wm.fetch_calls(client, 400)
        verdict = wm.optimization_verdict(calls)
        print("\n" + wm.render_breakdown(wm.role_breakdown(calls)))
        print("\nVERDICT:\n" + wm.render_verdict(verdict))

        change = apply_action(verdict)
        print(f"\n>> ACTION: {change['role']}: {change['change']}")

        print(f"\n[2/2] Re-run after optimization: {goal!r}")
        orch.run_blocking(goal, timeout=300)
        _flush()
        cost_after, run_after = _latest_run_cost(client)
    finally:
        orch.shutdown()

    pct = (1 - cost_after / cost_before) * 100 if cost_before else 0.0
    return {
        "goal": goal,
        "cost_before": cost_before,
        "cost_after": cost_after,
        "pct_saved": pct,
        "verdict": verdict,
        "action": change,
        "run_before": run_before,
        "run_after": run_after,
    }


def main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    args = [a for a in argv[1:] if not a.startswith("--")]
    rounds = 3
    for a in argv:
        if a.startswith("--rounds="):
            rounds = int(a.split("=", 1)[1])
    goal = " ".join(args).strip() or \
        "Research the top 5 AI note-taking apps and recommend one with reasons"

    res = optimize_loop(goal, rounds=rounds)
    print("\n" + "=" * 60)
    print("  SELF-IMPROVEMENT TREND (cost + quality, from Weave)")
    print("=" * 60)
    for t in res["trend"]:
        mark = "" if t["round"] == 0 else (" ✓kept" if t["kept"] else " ✗reverted")
        print(f"  round {t['round']}: {wm.fmt_usd(t['cost']):>10}  "
              f"quality {t['quality']:>3}/100  | {t['change']}{mark}")
    print("-" * 60)
    print(f"  cost   : {wm.fmt_usd(res['cost_before'])} → {wm.fmt_usd(res['cost_after'])} "
          f"({res['pct_saved']:.0f}% saved)")
    print(f"  quality: {res['quality_before']}/100 → {res['quality_after']}/100 "
          f"(guardrail held)")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

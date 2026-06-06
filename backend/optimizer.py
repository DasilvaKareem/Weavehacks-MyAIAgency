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
    goal = " ".join(argv[1:]).strip() or \
        "Research the top 5 AI note-taking apps and recommend one with reasons"
    res = optimize_once(goal)
    print("\n" + "=" * 56)
    print("  SELF-OPTIMIZATION RESULT (cost per goal, from Weave)")
    print("=" * 56)
    print(f"  before : {wm.fmt_usd(res['cost_before'])}")
    print(f"  after  : {wm.fmt_usd(res['cost_after'])}")
    print(f"  saved  : {res['pct_saved']:.0f}%")
    print(f"  fix    : {res['action']['role']} — {res['action']['change']}")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

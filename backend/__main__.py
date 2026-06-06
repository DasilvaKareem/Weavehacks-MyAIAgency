"""CLI demo:  python -m backend "Launch a developer-tools startup"

Loads .env (if python-dotenv is present), runs one company through the graph,
and prints each agent's result plus the CEO's executive summary.
"""
from __future__ import annotations

import sys

from .orchestrator import Orchestrator


def main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass  # .env is optional; env vars may already be set

    goal = " ".join(argv[1:]).strip() or "Launch a developer-tools startup"
    print(f"\n  CEO goal: {goal}\n  " + "-" * 50)

    orch = Orchestrator()
    try:
        future = orch.submit(goal)
        # Drain status events while the run proceeds on the backend thread.
        report = None
        while report is None:
            for ev in orch.poll_events():
                if ev.kind == "plan":
                    print(f"  Plan: {len(ev.payload)} tasks")
                    for t in ev.payload:
                        print(f"    - [{t.role}] {t.description}")
                elif ev.kind == "task_done":
                    r = ev.payload
                    mark = "ok" if r.status == "done" else "ERR"
                    print(f"  [{mark}] {r.role}: {(r.output or r.error)[:80]}")
                elif ev.kind == "report":
                    report = ev.payload
                elif ev.kind == "error":
                    print(f"  ERROR: {ev.payload}")
                    return 1
            if future.done() and report is None:
                report = future.result()
        print("\n  Executive summary\n  " + "-" * 50)
        print("  " + (report or "(none)").replace("\n", "\n  "))
        return 0
    finally:
        orch.shutdown()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

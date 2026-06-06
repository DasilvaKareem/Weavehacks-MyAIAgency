"""Async runtime that drives the company graph off the game thread.

The raylib loop must never block on a model call. The Orchestrator owns a
dedicated asyncio loop on a daemon thread; `submit()` is thread-safe and returns
immediately, and the game loop drains status events each frame with
`poll_events()` (non-blocking). This is the M3 bridge, built now so the design
is concurrency-correct from the start rather than retrofitted.

Standalone (no game) usage:

    orch = Orchestrator()
    report = orch.run_blocking("Launch a dev-tools startup")   # waits, returns report
    orch.shutdown()
"""
from __future__ import annotations

import asyncio
import queue
import threading
from dataclasses import dataclass
from typing import Any

from .graph import build_company_graph


@dataclass
class AgentEvent:
    """A status update streamed back toward the game loop."""

    kind: str          # "plan" | "task_started" | "task_done" | "report" | "error"
    payload: Any = None


class Orchestrator:
    def __init__(self) -> None:
        from .observability import init_weave

        init_weave()  # turn on Weave tracing (no-op without WANDB_API_KEY)
        self._graph = build_company_graph()
        self._events: "queue.Queue[AgentEvent]" = queue.Queue()
        self._active: set = set()   # in-flight run futures, for graceful shutdown
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="company-backend", daemon=True
        )
        self._thread.start()

    # --- thread plumbing ---------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, goal: str) -> "concurrent.futures.Future":
        """Thread-safe. Schedule a company run; returns a Future for the report."""
        import concurrent.futures  # noqa: F401  (typing only)

        future = asyncio.run_coroutine_threadsafe(self._execute(goal), self._loop)
        self._active.add(future)
        future.add_done_callback(self._active.discard)
        return future

    def run_blocking(self, goal: str, timeout: float | None = None) -> str:
        """Convenience for CLI/tests: submit and wait for the final report."""
        return self.submit(goal).result(timeout=timeout)

    def poll_events(self) -> list[AgentEvent]:
        """Drain all pending events without blocking. Call once per frame."""
        drained: list[AgentEvent] = []
        while True:
            try:
                drained.append(self._events.get_nowait())
            except queue.Empty:
                return drained

    def shutdown(self, drain_timeout: float = 5.0) -> None:
        """Let in-flight runs finish (up to drain_timeout), then stop the loop."""
        import concurrent.futures

        if self._active:
            concurrent.futures.wait(set(self._active), timeout=drain_timeout)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)

    # --- the run ------------------------------------------------------------

    async def _execute(self, goal: str) -> str:
        import uuid

        from .observability import tag

        report = ""
        run_id = uuid.uuid4().hex[:12]  # one id per company run, for cost_per_goal
        try:
            # Tag every call in this run with the goal + run id so the
            # Observability Engineer can compute cost-per-goal and compare runs.
            with tag(run_id=run_id, run_goal=goal, kind="company_run"):
                # Stream node-by-node so the game can show agents lighting up live.
                async for update in self._graph.astream({"goal": goal}):
                    for node, payload in update.items():
                        self._emit_for(node, payload)
                        if node == "ceo_review" and payload.get("report"):
                            report = payload["report"]
        except Exception as exc:
            self._events.put(AgentEvent("error", str(exc)))
            raise
        return report

    def _emit_for(self, node: str, payload: dict) -> None:
        if node == "ceo_plan":
            tasks = payload.get("tasks", [])
            self._events.put(AgentEvent("plan", tasks))
        elif node == "worker":
            for result in payload.get("results", []):
                self._events.put(AgentEvent("task_done", result))
        elif node == "ceo_review":
            self._events.put(AgentEvent("report", payload.get("report", "")))

"""The company graph — a map-reduce over agents.

    START -> ceo_plan -> (fan-out: one Send per task) -> worker* -> ceo_review -> END

`dispatch` returns a list of `Send`s, one per task. LangGraph runs the resulting
worker invocations concurrently and the `results` reducer merges them — so the
graph topology is fixed while the worker count scales with the plan. Add a 50th
agent and nothing here changes.
"""
from __future__ import annotations

from .agents import ceo_plan, ceo_review, worker
from .state import CompanyState


def _dispatch(state: CompanyState):
    """Conditional edge: fan out one worker per planned task."""
    from langgraph.types import Send

    return [
        Send("worker", {"goal": state["goal"], "task": task})
        for task in state.get("tasks", [])
    ]


def build_company_graph():
    """Compile and return the runnable company graph.

    Lazy import of langgraph keeps the Python >=3.10 dependency out of the path
    until the backend is actually built.
    """
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(CompanyState)
    builder.add_node("ceo_plan", ceo_plan)
    builder.add_node("worker", worker)
    builder.add_node("ceo_review", ceo_review)

    builder.add_edge(START, "ceo_plan")
    # Fan-out: ceo_plan -> N workers (dynamic, via Send).
    builder.add_conditional_edges("ceo_plan", _dispatch, ["worker"])
    # Fan-in: every worker -> review (reducer merges results first).
    builder.add_edge("worker", "ceo_review")
    builder.add_edge("ceo_review", END)

    return builder.compile()

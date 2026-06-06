"""Company.AI backend — scalable LangGraph + Gemini multi-agent orchestration.

Milestone M2. Designed to scale to many concurrent agents:

    graph.py         map-reduce graph: CEO plan -> fan-out to N workers -> review
    agents.py        stateless worker node logic, keyed by agent identity
    state.py         typed state schemas + reducers
    llm.py           cached Gemini client factory
    config.py        tunable backend constants (model, concurrency, timeouts)
    orchestrator.py  async runtime: bounded worker pool, runs off the game thread

The frontend (raylib, Python 3.9) and this backend (LangGraph, Python >=3.10)
are intentionally decoupled. M3 wires them together over a worker thread.

Quick demo (needs deps + GOOGLE_API_KEY):

    python -m backend "Launch a developer-tools startup"
"""
from __future__ import annotations

__all__ = ["build_company_graph", "Orchestrator", "Task", "Result", "CompanyState"]


def __getattr__(name: str):
    # Lazy re-exports so importing the package never pulls in langgraph/gemini
    # (and their Python >=3.10 requirement) until something is actually used.
    if name in ("build_company_graph",):
        from .graph import build_company_graph
        return build_company_graph
    if name in ("Task", "Result", "CompanyState"):
        from . import state
        return getattr(state, name)
    if name == "Orchestrator":
        from .orchestrator import Orchestrator
        return Orchestrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

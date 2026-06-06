"""Typed state schemas and reducers for the company graph.

Worker nodes are stateless: everything an agent needs (its identity, its task)
arrives in the state slice handed to it, and it returns only its own result.
The `results` reducer (`operator.add`) merges every worker's output back into
the shared state — this is what makes the fan-out a clean map-reduce that scales
with the number of agents.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Literal

from typing_extensions import TypedDict


@dataclass(frozen=True)
class Task:
    """One unit of work the CEO assigns to a single agent."""

    id: str
    role: str                 # e.g. "Engineer", "Researcher" — matches AGENT_ROLES
    description: str
    # Optional link to the on-screen Character.backend_id this task is meant for.
    agent_id: str | None = None


@dataclass(frozen=True)
class Result:
    """What one agent reports back for its task."""

    task_id: str
    role: str
    agent_id: str | None
    output: str
    status: Literal["done", "error"] = "done"
    error: str | None = None


class CompanyState(TypedDict, total=False):
    """Shared state threaded through the graph for a single company run."""

    goal: str                                   # CEO's high-level objective
    tasks: list[Task]                           # decomposed by ceo_plan
    # Reduced across all workers running in parallel (map-reduce):
    results: Annotated[list[Result], operator.add]
    report: str                                 # final CEO synthesis


class WorkerState(TypedDict):
    """State slice handed to a single worker via Send — one task, isolated."""

    goal: str
    task: Task

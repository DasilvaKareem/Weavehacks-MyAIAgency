"""Node logic for the company graph: CEO planner, worker, CEO reviewer.

Worker nodes are async and stateless, guarded by a module-level semaphore so
that hiring N agents never launches more than MAX_CONCURRENT_AGENTS live model
calls at once. That single knob is the scaling boundary.
"""
from __future__ import annotations

import asyncio
import json
import re

from . import config
from .llm import get_llm
from .mcp_bridge import run_tool_loop
from .persona import generate as make_persona, render_prompt as render_persona
from .state import CompanyState, Result, Task, WorkerState
from .tool_builder import build_tools

# Global concurrency gate shared by every worker across every active run.
_agent_semaphore: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    # Created lazily so it binds to the running loop, not import-time.
    global _agent_semaphore
    if _agent_semaphore is None:
        _agent_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_AGENTS)
    return _agent_semaphore


def _text(resp) -> str:
    """Flatten a model response to plain text.

    Newer Gemini models return `content` as a list of content blocks
    (dicts with a "text" key, or strings) rather than a bare string.
    """
    content = getattr(resp, "content", "") or ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _extract_json(text: str) -> list | dict | None:
    """Best-effort pull of a JSON array/object out of a model response."""
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    match = re.search(r"(\[.*\]|\{.*\})", candidate, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


# --- CEO: decompose the goal into per-agent tasks ---------------------------

_PLAN_PROMPT = """You are the CEO of an AI company. Break this goal into a short \
list of concrete subtasks, each assigned to one specialist agent.

Goal: {goal}

Available roles: Engineer, Researcher, Designer, Analyst, Marketer, DevOps, Sales, Recruiter.
Return ONLY a JSON array of at most {max_tasks} objects, each:
  {{"role": "<one of the roles>", "description": "<one specific task>"}}"""


async def ceo_plan(state: CompanyState) -> dict:
    goal = state["goal"]
    llm = get_llm()
    prompt = _PLAN_PROMPT.format(goal=goal, max_tasks=config.MAX_TASKS_PER_GOAL)
    resp = await llm.ainvoke(prompt)
    parsed = _extract_json(_text(resp))

    tasks: list[Task] = []
    if isinstance(parsed, list):
        for i, item in enumerate(parsed[: config.MAX_TASKS_PER_GOAL]):
            if not isinstance(item, dict):
                continue
            tasks.append(
                Task(
                    id=f"t{i + 1}",
                    role=str(item.get("role", "Engineer")),
                    description=str(item.get("description", "")).strip(),
                )
            )
    if not tasks:
        # Fallback so a malformed plan never stalls the run.
        tasks = [Task(id="t1", role="Engineer", description=goal)]
    return {"tasks": tasks}


# --- Worker: one agent executes one task ------------------------------------

_WORKER_PROMPT = """You are a {role} at an AI company working toward: {goal}

{persona}{profile}Your assigned task: {description}

You share a company drive (drive_list/drive_read/drive_search/drive_write): check \
it for anything a teammate already left that you need, and save any artifact \
worth keeping (a spec, draft, dataset, or plan) so the rest of the company can \
build on it. Then report your concrete result in under 120 words."""


async def worker(state: WorkerState) -> dict:
    task: Task = state["task"]
    llm = get_llm()
    profile = config.role_profile(task.role)
    # Seed the persona from the assigned agent when known, else the task id, so a
    # worker's character is stable and its work reflects its strengths.
    persona = render_persona(make_persona(task.agent_id or task.id, task.role))
    prompt = _WORKER_PROMPT.format(
        role=task.role, goal=state["goal"], description=task.description,
        persona=persona + "\n\n",
        profile=(profile + "\n\n") if profile else "",
    )
    # Every agent — profiled or not — gets the shared company drive (drive_* tools)
    # so any worker can leave artifacts for the rest of the company and pick up
    # what others left. A profiled role then ALSO gets the tools mapped to it:
    # Opsera (DevOps) / Apify (Researcher, Sales, …) MCP tools, the local shell/file
    # layer (when exec is enabled), and a Daytona cloud sandbox (Software Engineer).
    tools = await build_tools(task.role, task.agent_id, task.role)
    async with _semaphore():  # the scale gate
        try:
            if tools:
                output = await run_tool_loop(llm, [("human", prompt)], tools)
            else:
                resp = await llm.ainvoke(prompt)
                output = _text(resp).strip()
            result = Result(
                task_id=task.id, role=task.role, agent_id=task.agent_id, output=output
            )
        except Exception as exc:  # keep one agent's failure from sinking the run
            result = Result(
                task_id=task.id,
                role=task.role,
                agent_id=task.agent_id,
                output="",
                status="error",
                error=str(exc),
            )
    return {"results": [result]}


# --- CEO: synthesize all agent results --------------------------------------

_REVIEW_PROMPT = """You are the CEO. Your agents finished their tasks toward: {goal}

Results:
{results}

Write a brief executive summary (under 150 words) of what the company achieved."""


async def ceo_review(state: CompanyState) -> dict:
    results = state.get("results", [])
    joined = "\n".join(
        f"- [{r.role}] {r.output or r.error or '(no output)'}" for r in results
    )
    llm = get_llm()
    prompt = _REVIEW_PROMPT.format(goal=state["goal"], results=joined)
    resp = await llm.ainvoke(prompt)
    return {"report": _text(resp).strip()}

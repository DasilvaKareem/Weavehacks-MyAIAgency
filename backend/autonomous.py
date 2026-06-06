"""Execute one durable background run as a real hired agent."""
from __future__ import annotations

from . import company, config
from .approval_policy import wrap_tools
from .agents import _text
from .llm import get_llm
from .mcp_bridge import run_tool_loop_sync
from .persona import generate as make_persona, render_prompt as render_persona
from .store import AgentStore, JobRunRow
from .tool_builder import build_tools_sync


def _context(store: AgentStore, run: JobRunRow) -> str:
    chat = store.history(run.agent_id, limit=8)
    prior = [r for r in store.list_runs(limit=6, agent_id=run.agent_id)
             if r.id != run.id and r.report]
    parts = []
    if chat:
        parts.append("Recent CEO conversation:\n" + "\n".join(
            f"- {m.role}: {m.content[:500]}" for m in chat
        ))
    if prior:
        parts.append("Recent autonomous activity:\n" + "\n".join(
            f"- {r.created_at}: {r.report[:500]}" for r in prior[:4]
        ))
    if run.denial_context:
        parts.append("CEO decision from the previous attempt:\n- " + run.denial_context)
    return "\n\n".join(parts) or "(no prior context)"


def execute_run(store: AgentStore, run: JobRunRow) -> str:
    agent = store.get(run.agent_id)
    if agent is None or agent.status == "fired":
        raise RuntimeError("assigned agent is no longer active")
    persona = render_persona(make_persona(agent.id, agent.role))
    profile = config.role_profile(agent.role)
    system = (
        f"You are {agent.name}, a {agent.role} at Company.AI. You are completing "
        "an autonomous background job for the CEO. Use your tools when useful, "
        "save durable artifacts to the shared company drive, and report the "
        "concrete outcome concisely. Never claim an external action succeeded "
        "unless a tool confirmed it.\n\n" + persona
    )
    if profile:
        system += "\n\n" + profile
    company_ctx = company.context_for(store)            # the CEO's company decisions
    if company_ctx:
        system += "\n\n" + company_ctx
    human = (
        f"Scheduled instruction:\n{run.instruction}\n\n"
        f"Context:\n{_context(store, run)}\n\n"
        "Complete the instruction now. End with a brief report for the CEO."
    )
    tools = build_tools_sync(agent.role, agent.id, agent.name)
    tools = wrap_tools(tools, store, run.id, agent.trust_tier)
    llm = get_llm(model=agent.model or config.GEMINI_MODEL)
    if tools:
        return run_tool_loop_sync(llm, [("system", system), ("human", human)], tools).strip()
    return _text(llm.invoke([("system", system), ("human", human)])).strip()

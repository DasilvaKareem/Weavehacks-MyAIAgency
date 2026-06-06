"""Dynamic agent-to-agent delegation — true handoffs.

Any agent can hand a specific subtask to another role mid-task and get the result
back to use. The teammate is run as a one-shot sub-agent (with its own full
toolset), so the handoff actually produces real work — not just a message. The
request and reply also flow over the Redis bus (visible/traceable as A2A comms),
and the whole sub-run is traced to Weave under kind="delegation".

Safety: handoffs are DEPTH-LIMITED (A→B→C max) so two agents can't ping-pong
forever, and each sub-agent runs in its OWN thread with a copied context — which
also dodges the "asyncio.run() inside a running loop" problem when delegation is
triggered from the async company graph.
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import logging

log = logging.getLogger("company.delegate")

MAX_DEPTH = 2          # A delegates to B delegates to C; C can't delegate further
MAX_TOTAL = 8          # hard cap on handoffs in a single chain
TIMEOUT_S = 180        # don't let a sub-agent hang the caller forever

_DEPTH = contextvars.ContextVar("deleg_depth", default=0)
_COUNT = contextvars.ContextVar("deleg_count", default=0)


def run_agent_once(role: str, subtask: str, requester: str = "") -> str:
    """Run `role` as a fresh one-shot agent on `subtask`; return its result text.

    Builds the role's full toolset (so a delegated Analyst can still scrape, a
    delegated Engineer can still run code, etc.) and respects any HR/optimizer
    retune for that role.
    """
    from . import company, config, role_policy
    from .agents import _text
    from .llm import get_llm
    from .mcp_bridge import run_tool_loop_sync
    from .observability import tag
    from .persona import generate as make_persona, render_prompt as render_persona
    from .store import AgentStore
    from .tool_builder import build_tools_sync

    model = role_policy.model(role)
    llm = get_llm(model=model) if model else get_llm()
    system = (
        f"You are a {role} at an AI company. A teammate ({requester or 'a colleague'}) "
        f"has delegated a specific subtask to you. Do it using your tools, then reply "
        f"with ONLY the concrete result they need — tight, no preamble."
    )
    system += "\n\n" + render_persona(make_persona(role, role))
    prof = config.role_profile(role)
    if prof:
        system += "\n\n" + prof
    cc = company.context_for(AgentStore())
    if cc:
        system += "\n\n" + cc

    tools = build_tools_sync(role, None, role)
    msgs = [("system", system), ("human", subtask)]
    with tag(role=role, kind="delegation"):
        if tools:
            return run_tool_loop_sync(
                llm, msgs, tools, max_steps=role_policy.max_steps(role)).strip()
        return _text(llm.invoke(msgs)).strip()


def delegate(to: str, subtask: str, from_role: str = "", from_id: str = "") -> str:
    """Hand `subtask` to role `to`, wait for the result, return it. Depth-limited."""
    depth, count = _DEPTH.get(), _COUNT.get()
    if depth >= MAX_DEPTH or count >= MAX_TOTAL:
        return ("[delegation limit reached — please complete this part yourself "
                "instead of handing it off further]")

    from . import agent_bus
    agent_bus.send(to, f"[handoff request] {subtask}",
                   from_name=from_role or "teammate", from_id=from_id)

    # Run the teammate in its own thread with a copied context, so depth/count
    # propagate, and asyncio.run() inside the sub-run gets a clean thread.
    ctx = contextvars.copy_context()

    def _run() -> str:
        _DEPTH.set(depth + 1)
        _COUNT.set(count + 1)
        return run_agent_once(to, subtask, requester=from_role)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            answer = ex.submit(ctx.run, _run).result(timeout=TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        answer = f"[{to} didn't respond in time — proceed without their input]"
    except Exception as exc:
        log.warning("delegation to %s failed: %s", to, exc)
        answer = f"[couldn't reach {to}: {exc}]"

    agent_bus.send(from_role or from_id, f"[handoff reply from {to}] {answer[:400]}",
                   from_name=to)
    return answer


def load_delegation_tools(agent_id: str | None, agent_name: str = "",
                          role: str = "") -> list:
    """The delegate_to tool for one agent. Always available (the sub-agent runs
    regardless of Redis); the bus just makes the handoff visible when configured."""
    from langchain_core.tools import tool

    @tool
    def delegate_to(to: str, subtask: str) -> str:
        """Hand a specific subtask to a teammate by ROLE (e.g. 'Analyst',
        'Engineer', 'Researcher') and get their result back to use in your own
        work. Use this when your task genuinely needs another specialist — delegate
        ONLY the specific piece you need, not your whole task. They'll do it with
        their own tools and reply with the result."""
        return delegate(to, subtask, from_role=role or agent_name, from_id=agent_id or "")

    return [delegate_to]

"""Bridge LangGraph workers to external MCP tool servers (Opsera).

This is what turns a DevOps *persona* into a DevOps *worker*: when an Opsera MCP
server is configured, profiled agents can call the real tools (architecture
analysis, security/SQL scans, compliance audits, DORA metrics, CI/CD) instead of
only describing them.

The bridge is OPTIONAL and degrades gracefully. With no server configured (or
the client libs missing, or the server unreachable), `load_tools()` returns an
empty list and callers fall back to prompt-only behaviour — so the game always
runs, with or without credentials.

Configure via env (see backend/config.py):
    OPSERA_MCP_URL    streamable-HTTP endpoint of the Opsera MCP server
    OPSERA_MCP_TOKEN  bearer token sent in the Authorization header
"""
from __future__ import annotations

import asyncio
import logging

from . import config

log = logging.getLogger("company.mcp")

# Known MCP servers: name -> () => (url, token). A server is "configured" only
# when both its url and token are present. Add new servers here.
# name -> (apify_actors) => (url, token). A server is "configured" only when both
# its url and token are present. The apify URL is actor-specific, so its getter
# takes the role's actor list (None = the default actor). Add new servers here.
_SERVERS = {
    "opsera": lambda actors=None: (config.OPSERA_MCP_URL, config.OPSERA_MCP_TOKEN),
    "apify": lambda actors=None: (config.apify_mcp_url(actors), config.APIFY_TOKEN),
}

# Cache loaded tools per (server-set, apify actors), so each role's toolset
# connects only once but different actor sets don't collide.
_tools_cache: dict = {}
_lock: asyncio.Lock | None = None


def _connection(name: str, apify_actors=None) -> dict | None:
    getter = _SERVERS.get(name)
    if getter is None:
        return None
    url, token = getter(apify_actors)
    if not (url and token):
        return None
    return {
        "transport": "streamable_http",
        "url": url,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def configured_servers() -> list:
    """Names of MCP servers that currently have both a URL and a token."""
    return [name for name in _SERVERS if _connection(name) is not None]


def is_configured(name: str | None = None) -> bool:
    """True if a given server is configured, or any server when name is None."""
    if name is not None:
        return _connection(name) is not None
    return bool(configured_servers())


def _get_lock() -> asyncio.Lock:
    # Lazily bound to the running loop, like the worker semaphore.
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def load_tools(servers=None, apify_actors=None) -> list:
    """LangChain tools from the given (configured) MCP servers; [] if none.

    `servers` is a list of server names (e.g. ['apify']); None means every known
    server. `apify_actors` selects which Apify actors to expose as tools for this
    call. Unconfigured or unreachable servers are skipped, never fatal. Cached
    per (server-set, actor-set) so each role's toolset connects at most once.
    """
    names = list(servers) if servers is not None else list(_SERVERS)
    conns = {n: _connection(n, apify_actors) for n in names
             if _connection(n, apify_actors) is not None}
    if not conns:
        return []

    key = (frozenset(conns), tuple(apify_actors or ()))
    if key in _tools_cache:
        return _tools_cache[key]

    async with _get_lock():
        if key in _tools_cache:  # filled while we waited
            return _tools_cache[key]
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient

            client = MultiServerMCPClient(conns)
            tools = await client.get_tools()
            log.info("Loaded %d MCP tool(s) from %s: %s", len(tools),
                     ", ".join(sorted(conns)), ", ".join(t.name for t in tools))
        except Exception as exc:  # missing libs, auth, network — never fatal
            log.warning("MCP servers %s unavailable (%s); prompt-only fallback.",
                        sorted(conns), exc)
            tools = []
        _tools_cache[key] = tools
        return tools


def _flatten(message) -> str:
    """Flatten a model message's content to plain text (str | list-of-blocks)."""
    content = getattr(message, "content", "") if message is not None else ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            parts.append(str(block.get("text", "")))
    return "".join(parts)


# Vendor/server prefixes stripped from a tool name so a progress label reads
# about the *action* ("using security scan") rather than the plumbing.
_TOOL_PREFIXES = {"opsera", "apify", "daytona", "composio", "mcp", "exec", "hr"}


def _humanize(name: str) -> str:
    """Turn a raw tool name ('OPSERA_SECURITY_SCAN') into a readable phrase."""
    words = name.replace("-", " ").replace("_", " ").split()
    if len(words) > 1 and words[0].lower() in _TOOL_PREFIXES:
        words = words[1:]
    return " ".join(words).strip().lower() or "a tool"


async def _stream_round(bound, convo, on_token):
    """Run one model turn, forwarding text deltas, and return the full message.

    With no `on_token` this is a plain `ainvoke` (unchanged behaviour). With one,
    it streams: each text delta is handed to `on_token` as it arrives, while the
    chunks are accumulated into the complete message (so `.tool_calls` is intact
    for the caller). Falls back to `ainvoke` if the stream yields nothing.
    """
    if on_token is None:
        return await bound.ainvoke(convo)
    full = None
    async for chunk in bound.astream(convo):
        full = chunk if full is None else full + chunk
        text = _flatten(chunk)
        if text:
            on_token(text)
    return full if full is not None else await bound.ainvoke(convo)


async def run_tool_loop(llm, messages, tools, max_steps: int | None = None,
                        on_step=None, on_token=None) -> str:
    """Let the model call MCP tools, feeding results back, until it answers.

    Bounded by `max_steps` model<->tool round-trips so a confused agent can't
    loop forever. Returns the model's final text.

    `on_step`, if given, is called with a short human-readable label each time
    the agent's activity changes — "thinking" while the model reasons, and
    "using <tool>" just before each tool call — so the UI can show what the
    agent is *actually* doing instead of a generic spinner.

    `on_token`, if given, streams the *final answer* token-by-token: it receives
    each text delta, and `on_token(None)` whenever a round turns out to be a tool
    call (a signal to discard any partial preamble, since the real answer comes
    later). Only the last, tool-free round survives as the streamed message.

    Both run on this thread, so the callbacks must be cheap and thread-safe
    (e.g. Queue.put).
    """
    from langchain_core.messages import ToolMessage

    max_steps = max_steps or config.MCP_MAX_TOOL_STEPS
    bound = llm.bind_tools(tools)
    by_name = {t.name: t for t in tools}
    convo = list(messages)
    last = None

    for step in range(max_steps):
        if on_step and step:           # not the first round: reasoning over results
            on_step("thinking")
        last = await _stream_round(bound, convo, on_token)
        convo.append(last)
        calls = getattr(last, "tool_calls", None) or []
        if not calls:
            break
        if on_token:                   # this round's text was preamble, not the answer
            on_token(None)
        for call in calls:
            tool = by_name.get(call["name"])
            if tool is None:
                convo.append(ToolMessage(
                    content=f"Unknown tool: {call['name']}", tool_call_id=call["id"]))
                continue
            if on_step:
                on_step(f"using {_humanize(call['name'])}")
            try:
                result = await tool.ainvoke(call.get("args", {}))
            except Exception as exc:
                # Autonomous runs deliberately pause before guarded side effects.
                # Keep that control-flow signal intact for worker_service.
                from .approval_policy import ApprovalRequired
                if isinstance(exc, ApprovalRequired):
                    raise
                result = f"[tool error: {exc}]"
            convo.append(ToolMessage(content=str(result), tool_call_id=call["id"]))

    final = _flatten(last).strip()
    if final or not (getattr(last, "tool_calls", None) or []):
        return final
    # The step budget ran out while the model was STILL calling tools and it never
    # wrote a closing answer — returning "" here is what made the terminal/chat look
    # dead. Do one final, tool-free round (unbound llm) so the model must turn the
    # tool results it already has into a real answer instead of an empty reply.
    if on_step:
        on_step("wrapping up")
    if on_token:
        on_token(None)             # drop any preamble streamed during the last round
    convo.append(("human",
                  "You've hit the tool-call limit for this turn. Stop calling tools "
                  "and give your best final answer now, using what you already have."))
    last = await _stream_round(llm, convo, on_token)
    return _flatten(last).strip()


def run_tool_loop_sync(llm, messages, tools, max_steps: int | None = None,
                       on_step=None, on_token=None) -> str:
    """Synchronous wrapper for callers off the event loop (e.g. the chat thread).

    Runs on the shared process-wide loop (async_loop) rather than a throwaway
    ``asyncio.run()`` loop, so the cached Gemini client's aiohttp session stays
    bound to a live loop across calls — otherwise the 2nd+ reply crashes with
    "Timeout context manager should be used inside a task" (the "100% crash" bug).
    """
    from .async_loop import run as _run_async
    return _run_async(
        run_tool_loop(llm, messages, tools, max_steps, on_step, on_token))


def load_tools_sync(servers=None, apify_actors=None) -> list:
    """Synchronous tool load for callers off the event loop."""
    from .async_loop import run as _run_async
    return _run_async(load_tools(servers, apify_actors))

"""Talk to a single hired agent — one-on-one, with persistent memory.

Each agent's identity (name + role) and full conversation live in the SQLite
store, so a chat picks up exactly where you left off, even across restarts.

CLI:
    python -m backend.chat                       # list your agents
    python -m backend.chat hire "Ada" Engineer   # hire one, prints its id
    python -m backend.chat <agent_id>            # interactive chat (Ctrl-D to exit)
"""
from __future__ import annotations

import sys

from .agents import _text          # response-content normalizer (str | list-of-blocks)
from . import company
from .config import GEMINI_MODEL, role_profile
from .llm import get_llm
from . import role_policy
from .mcp_bridge import run_tool_loop_sync
from .observability import tag, traced
from .persona import generate as make_persona, render_prompt as render_persona
from .store import AgentStore
from .tool_builder import build_tools_sync

# How many past turns to replay into the prompt (bounds token cost per message).
HISTORY_WINDOW = 20

_PERSONA = (
    "You are {name}, a {role} at an AI company called Company.AI. "
    "You report directly to the CEO, who is the person talking to you now. "
    "Stay in character as {name}. Speak in the first person about your own work, "
    "be concrete and concise, and ask for clarification when a request is ambiguous."
)


@traced
def chat_attempt(llm, msgs, tools, on_step, on_token, max_steps=None) -> str:
    """One hired agent's reply attempt — traced as its own op so Weave records
    THIS agent's real cost/latency (and any crash) tagged to their id. Raises on
    hard failure; AgentChat.send catches it outside to salvage partial streams."""
    if tools:
        return run_tool_loop_sync(llm, msgs, tools, max_steps=max_steps,
                                  on_step=on_step, on_token=on_token).strip()
    return _text(llm.invoke(msgs)).strip()


class AgentChat:
    """A stateful conversation with one agent, backed by the SQL store."""

    def __init__(self, agent_id: str, store: AgentStore | None = None) -> None:
        self.store = store or AgentStore()
        agent = self.store.get(agent_id)
        if agent is None or agent.status == "fired":
            raise ValueError(f"No active agent with id {agent_id!r}")
        self.agent = agent
        self.llm = get_llm(model=agent.model or GEMINI_MODEL)

    def send(self, message: str, persist_user: bool = True,
             on_step=None, on_token=None) -> str:
        """Send one message to the agent; persist both sides; return the reply.

        The human message is persisted BEFORE the (possibly minutes-long) model
        call, so the prompt is durable the instant work starts and shows in the
        UI immediately. Pass persist_user=False when the caller already stored it
        (e.g. CompanyLink, which persists synchronously so the panel echoes the
        message right away). Only the AI reply is written after the call returns.

        `on_step`, if given, receives a short label each time the agent's
        activity changes during a tool-using turn ("thinking", "using <tool>"),
        so the UI can show real progress. Plain completions never call tools, so
        it stays silent there — the panel keeps its default verb.

        `on_token`, if given, streams the final answer token-by-token to the UI
        (with `on_token(None)` resets between tool rounds). Tokens are mirrored
        into a local buffer so that if the model call fails mid-stream, the
        partial answer is persisted and returned rather than lost; a failure
        with nothing streamed yet propagates as before.
        """
        if persist_user:
            self.store.add_message(self.agent.id, "human", message)
        # History now already ends with this human message, so we don't append it
        # again below — building the prompt straight from the durable record.
        history = self.store.history(self.agent.id, limit=HISTORY_WINDOW)
        system = _PERSONA.format(name=self.agent.name, role=self.agent.role)
        system += "\n\n" + render_persona(make_persona(self.agent.id, self.agent.role))
        profile = role_profile(self.agent.role)
        if profile:
            system += "\n\n" + profile
        company_ctx = company.context_for(self.store)   # the CEO's company decisions
        if company_ctx:
            system += "\n\n" + company_ctx
        msgs = [("system", system)]
        msgs += [(m.role, m.content) for m in history]
        if not history or history[-1].content != message:
            msgs.append(("human", message))   # safety net if it wasn't persisted

        # Every agent gets the shared company drive (drive_* tools) so a 1:1 chat
        # can save and recall artifacts alongside the rest of the company. A
        # profiled agent then ALSO gets the tools mapped to it: MCP (Opsera/Apify),
        # the local shell/file layer (when exec is enabled), and a Daytona cloud
        # sandbox (Software Engineer). Runs on the chat worker thread, so a fresh
        # event loop is safe here.
        tools = build_tools_sync(self.agent.role, self.agent.id, self.agent.name)
        # Mirror streamed tokens into a local buffer; a reset (None) clears it,
        # so it only ever holds the current/final answer — what we salvage if the
        # call dies mid-stream.
        collected: list[str] = []

        def _emit(tok):
            if tok is None:
                collected.clear()
            else:
                collected.append(tok)
            if on_token:
                on_token(tok)

        # Respect any HR/optimizer retune for this agent's role (cheaper model /
        # smaller tool budget) — this is how HR's self-improvement actually changes
        # what the agent costs on its next turn.
        pol_model = role_policy.model(self.agent.role)
        llm = get_llm(model=pol_model) if pol_model else self.llm
        pol_steps = role_policy.max_steps(self.agent.role)
        # Tag this turn with the hired agent's identity so its real traces are
        # attributed to THEM in Weave — the data HR reads to fire/repurpose.
        with tag(agent_id=self.agent.id, agent_name=self.agent.name,
                 role=self.agent.role, kind="chat"):
            try:
                reply = chat_attempt(llm, msgs, tools, on_step, _emit, max_steps=pol_steps)
            except Exception:
                partial = "".join(collected).strip()
                if not partial:
                    raise                # nothing salvageable → behave as before
                reply = f"{partial}\n\n_[interrupted before finishing]_"

        # Persist the exchange so the next turn (and next session) remembers it.
        self.store.add_message(self.agent.id, "ai", reply)
        return reply


# --- CLI --------------------------------------------------------------------

def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def _cmd_list(store: AgentStore) -> int:
    agents = store.list_agents()
    if not agents:
        print("No agents hired yet. Hire one:")
        print('    python -m backend.chat hire "Ada" Engineer')
        return 0
    print("Your agents:")
    for a in agents:
        print(f"  {a.id}  {a.name:<16} {a.role:<12} [{a.status}]  hired {a.hired_at}")
    print("\nChat with one:  python -m backend.chat <agent_id>")
    return 0


def _cmd_hire(store: AgentStore, argv: list[str]) -> int:
    if len(argv) < 4:
        print('Usage: python -m backend.chat hire "<name>" <role>')
        return 2
    name, role = argv[2], argv[3]
    agent = store.hire(name=name, role=role, model=GEMINI_MODEL)
    print(f"Hired {agent.name} ({agent.role}) -> id {agent.id}")
    print(f"Talk to them:  python -m backend.chat {agent.id}")
    return 0


def _cmd_chat(store: AgentStore, agent_id: str) -> int:
    try:
        chat = AgentChat(agent_id, store=store)
    except ValueError as exc:
        print(exc)
        return 1
    a = chat.agent
    print(f"— Talking to {a.name} ({a.role}). Ctrl-D or 'exit' to leave. —\n")
    while True:
        try:
            msg = input("CEO > ").strip()
        except EOFError:
            print()
            break
        if msg.lower() in {"exit", "quit"}:
            break
        if not msg:
            continue
        store.set_status(a.id, "working")
        reply = chat.send(msg)
        store.set_status(a.id, "idle")
        print(f"{a.name} > {reply}\n")
    return 0


def main(argv: list[str]) -> int:
    _load_env()
    from .observability import init_weave

    init_weave()  # trace 1:1 chats too (no-op without WANDB_API_KEY)
    store = AgentStore()
    if len(argv) == 1:
        return _cmd_list(store)
    if argv[1] == "hire":
        return _cmd_hire(store, argv)
    if argv[1] in {"list", "ls"}:
        return _cmd_list(store)
    return _cmd_chat(store, argv[1])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

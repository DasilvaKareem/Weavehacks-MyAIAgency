"""Fire-and-forget task firehose, backed by Redis.

Drop tasks as fast as you like — they go onto a Redis queue and a pool of workers
pulls them off and runs them as real agents, concurrently (bounded by
MAX_CONCURRENT_AGENTS), posting results back. No blocking, no "agent busy": you
keep going and the team chews through the backlog.

Each task is claimed atomically (Redis LPOP), assigned to an idle hired agent
(matching the requested role when given), executed with that agent's full toolset,
and traced to Weave (kind="task", attributed to the agent). Falls back to an
in-memory queue when REDIS_URL isn't set, so it still works offline.
"""
from __future__ import annotations

import collections
import concurrent.futures
import json
import logging
import threading
import time
import uuid

from . import config

log = logging.getLogger("company.taskq")

_mem: "collections.deque[str]" = collections.deque()   # offline fallback queue
_mem_lock = threading.Lock()


def _key() -> str:
    from .agent_bus import _ns
    return f"{_ns()}:taskq"


def _redis():
    from .agent_bus import _redis as _r
    return _r()


# --- queue ops --------------------------------------------------------------

def enqueue(text: str, role: str = "") -> str:
    """Add a task to the back of the queue. Returns immediately with a task id."""
    tid = uuid.uuid4().hex[:12]
    item = json.dumps({"id": tid, "text": text, "role": role, "ts": time.time()})
    r = _redis()
    if r is not None:
        try:
            r.rpush(_key(), item)
            return tid
        except Exception as exc:
            log.warning("redis enqueue failed (%s); using memory queue", exc)
    with _mem_lock:
        _mem.append(item)
    return tid


def claim() -> dict | None:
    """Atomically take the next task off the front of the queue, or None if empty."""
    r = _redis()
    if r is not None:
        try:
            v = r.lpop(_key())
            return json.loads(v) if v else None
        except Exception as exc:
            log.warning("redis claim failed (%s); using memory queue", exc)
    with _mem_lock:
        return json.loads(_mem.popleft()) if _mem else None


def pending() -> int:
    r = _redis()
    if r is not None:
        try:
            return int(r.llen(_key()))
        except Exception:
            pass
    with _mem_lock:
        return len(_mem)


# --- dispatcher -------------------------------------------------------------

class Dispatcher:
    """Pulls tasks off the queue and runs them as agents, concurrently. Runs on a
    daemon thread; call start()/stop(). `on_result(task, result, agent)` fires when
    each task finishes (e.g. to post into the CEO's inbox)."""

    def __init__(self, store=None, on_result=None, max_workers: int | None = None,
                 poll: float = 0.5) -> None:
        self.store = store
        self.on_result = on_result
        self.max_workers = max_workers or config.MAX_CONCURRENT_AGENTS
        self.poll = poll
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="taskq")
        self._active: set = set()
        self._run = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._run:
            return
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="taskq-dispatch")
        self._thread.start()

    def stop(self) -> None:
        self._run = False

    def drain_once(self) -> int:
        """Claim and run every currently-queued task, wait for them, return the
        count. Used by `worker_service --once`."""
        futs = []
        while True:
            task = claim()
            if task is None:
                break
            futs.append(self._pool.submit(self._do, task))
        concurrent.futures.wait(futs)
        return len(futs)

    def _loop(self) -> None:
        while self._run:
            self._active = {f for f in self._active if not f.done()}
            while len(self._active) < self.max_workers:
                task = claim()
                if task is None:
                    break
                self._active.add(self._pool.submit(self._do, task))
            time.sleep(self.poll)

    def _do(self, task: dict):
        from .store import AgentStore
        store = self.store or AgentStore()
        agent = self._pick_agent(store, task.get("role", ""))
        result = self._execute(store, agent, task)
        if self.on_result:
            try:
                self.on_result(task, result, agent)
            except Exception as exc:  # a bad callback must not kill the worker
                log.warning("on_result failed: %s", exc)
        return result

    def _pick_agent(self, store, role: str):
        """An idle hired agent (matching the role if given), else any active one."""
        from .weave_metrics import canon_role
        agents = [a for a in store.list_agents() if a.status != "fired"]
        idle = [a for a in agents if a.status == "idle"] or agents
        if role:
            want = canon_role(role)
            for a in idle:
                if canon_role(a.role) == want:
                    return a
        return idle[0] if idle else None

    def _execute(self, store, agent, task: dict) -> str:
        from . import company, role_policy
        from .agents import _text
        from .llm import get_llm
        from .mcp_bridge import run_tool_loop_sync
        from .observability import tag
        from .persona import generate as make_persona, render_prompt as render_persona
        from .tool_builder import build_tools_sync

        role = (agent.role if agent else task.get("role", "")) or "Generalist"
        aid = agent.id if agent else ""
        name = agent.name if agent else role
        if agent:
            store.set_status(agent.id, "working")   # bot sits + works in the 3D office
        try:
            model = (role_policy.model(role)
                     or (agent.model if agent else None) or config.GEMINI_MODEL)
            llm = get_llm(model=model)
            system = (
                f"You are {name}, a {role} at Company.AI. A task came in off the team "
                f"queue. Use your tools, save any durable artifact to the shared drive, "
                f"and report the concrete outcome concisely.\n\n"
                + render_persona(make_persona(aid or role, role))
            )
            prof = config.role_profile(role)
            if prof:
                system += "\n\n" + prof
            cc = company.context_for(store)
            if cc:
                system += "\n\n" + cc
            tools = build_tools_sync(role, aid or None, name)
            msgs = [("system", system), ("human", task.get("text", ""))]
            with tag(agent_id=aid, agent_name=name, role=role, kind="task"):
                if tools:
                    return run_tool_loop_sync(
                        llm, msgs, tools, max_steps=role_policy.max_steps(role)).strip()
                return _text(llm.invoke(msgs)).strip()
        except Exception as exc:
            return f"[task failed: {exc}]"
        finally:
            if agent:
                store.set_status(agent.id, "idle")


# --- CLI: enqueue, or drain the queue ---------------------------------------

def main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    if len(argv) > 1 and argv[1] == "work":
        d = Dispatcher(on_result=lambda t, r, a: print(
            f"  ✓ [{(a.name if a else 'role:'+t.get('role','?'))}] "
            f"{t['text'][:44]} → {r[:90]}"))
        d.start()
        print(f"Draining task queue ({pending()} pending). Ctrl-C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            d.stop()
            print("\nstopped.")
        return 0
    text = " ".join(argv[1:]).strip() or "Say hello to the team and note the time."
    tid = enqueue(text)
    print(f"enqueued {tid}: {text}  (pending={pending()})")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))

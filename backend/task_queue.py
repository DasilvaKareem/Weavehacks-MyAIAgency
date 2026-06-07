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

# A Redis Stream + consumer group, not a plain list. The difference is reliability:
# a list LPOP hands a task to a worker and immediately forgets it, so if that worker
# crashes mid-task the work is lost. A consumer group keeps every claimed task in a
# Pending Entries List until the worker XACKs it; if the worker dies, another worker
# reclaims it via XAUTOCLAIM. So tasks survive crashes — exactly what an always-on
# firehose needs. Falls back to a list, then an in-memory deque, when Redis is off.
_GROUP = "workers"
_grp_ready = False


def _key() -> str:
    from .agent_bus import _ns
    return f"{_ns()}:taskstream"


def _list_key() -> str:   # legacy/fallback list, still drained for back-compat
    from .agent_bus import _ns
    return f"{_ns()}:taskq"


def _redis():
    from .agent_bus import _redis as _r
    return _r()


def _ensure_group(r) -> bool:
    """Create the stream + consumer group once (idempotent). False if it can't."""
    global _grp_ready
    if _grp_ready:
        return True
    try:
        r.xgroup_create(_key(), _GROUP, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):   # already exists is fine
            log.warning("xgroup_create failed (%s)", exc)
            return False
    _grp_ready = True
    return True


# --- queue ops --------------------------------------------------------------

def enqueue(text: str, role: str = "", source: str = "") -> str:
    """Add a task to the queue. Returns immediately with a task id.

    `source` is a free-form origin tag (e.g. 'terminal') carried through to the
    result callback, so the dispatcher can route outcomes back where they came
    from — terminal-fired work echoes into the terminal log."""
    tid = uuid.uuid4().hex[:12]
    payload = json.dumps({"id": tid, "text": text, "role": role,
                          "source": source, "ts": time.time()})
    r = _redis()
    if r is not None:
        try:
            r.xadd(_key(), {"payload": payload})
            return tid
        except Exception as exc:
            log.warning("redis enqueue failed (%s); using memory queue", exc)
    with _mem_lock:
        _mem.append(payload)
    return tid


def _parse(msg_id: str, fields: dict) -> dict:
    task = json.loads(fields.get("payload", "{}"))
    task["_msgid"] = msg_id    # carried so the worker can XACK after finishing
    return task


def claim(consumer: str = "worker") -> dict | None:
    """Take the next task, or None if empty. From a Redis Stream consumer group
    when available — first reclaiming any task abandoned by a dead worker
    (XAUTOCLAIM), then reading a fresh one — else the fallback list / memory."""
    r = _redis()
    if r is not None and _ensure_group(r):
        try:
            # 1) Reclaim work a crashed worker left pending for >60s.
            res = r.execute_command("XAUTOCLAIM", _key(), _GROUP, consumer,
                                    "60000", "0", "COUNT", "1")
            claimed = res[1] if len(res) > 1 else []
            if claimed:
                mid, fields = claimed[0]
                if fields:
                    log.info("reclaimed abandoned task %s", mid)
                    return _parse(mid, fields)
                r.xack(_key(), _GROUP, mid)   # tombstone (already deleted) — drop it
            # 2) Otherwise read a brand-new task.
            resp = r.xreadgroup(_GROUP, consumer, {_key(): ">"}, count=1)
            if resp:
                _, entries = resp[0]
                if entries:
                    mid, fields = entries[0]
                    return _parse(mid, fields)
            # 3) Drain any leftovers from the legacy list, for back-compat.
            v = r.lpop(_list_key())
            if v:
                return json.loads(v)
            return None
        except Exception as exc:
            log.warning("redis claim failed (%s); using memory queue", exc)
    with _mem_lock:
        return json.loads(_mem.popleft()) if _mem else None


def ack(task: dict | None) -> None:
    """Confirm a task finished so the group stops tracking it (and trim the stream).
    No-op for fallback-queue tasks (which carry no _msgid)."""
    if not task or not task.get("_msgid"):
        return
    r = _redis()
    if r is None:
        return
    try:
        r.xack(_key(), _GROUP, task["_msgid"])
        r.xdel(_key(), task["_msgid"])        # bound memory — done means gone
    except Exception as exc:
        log.warning("ack failed: %s", exc)


def pending() -> int:
    """Tasks not yet acknowledged (new + in-flight), plus any fallback entries."""
    r = _redis()
    if r is not None:
        try:
            n = int(r.xlen(_key())) if _grp_ready or _ensure_group(r) else 0
            try:
                n += int(r.llen(_list_key()))
            except Exception:
                pass
            return n
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
        # A stable per-process consumer name so XAUTOCLAIM can tell whose pending
        # entries are whose, and reclaim a dead process's tasks.
        import socket
        self.consumer = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"

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
            task = claim(self.consumer)
            if task is None:
                break
            futs.append(self._pool.submit(self._do, task))
        concurrent.futures.wait(futs)
        return len(futs)

    def _loop(self) -> None:
        while self._run:
            self._active = {f for f in self._active if not f.done()}
            while len(self._active) < self.max_workers:
                task = claim(self.consumer)
                if task is None:
                    break
                self._active.add(self._pool.submit(self._do, task))
            time.sleep(self.poll)

    def _do(self, task: dict):
        from .store import AgentStore
        store = self.store or AgentStore()
        agent = self._pick_agent(store, task.get("role", ""))
        try:
            result = self._execute(store, agent, task)
            if self.on_result:
                try:
                    self.on_result(task, result, agent)
                except Exception as exc:  # a bad callback must not kill the worker
                    log.warning("on_result failed: %s", exc)
            self._remember(agent, task, result)
            return result
        finally:
            # Acknowledge regardless of logical outcome — _execute swallows its own
            # errors, so a finished _do means this task is handled. (Crash recovery
            # is for a *dead worker*, not a task that merely failed.)
            ack(task)

    def _remember(self, agent, task: dict, result: str) -> None:
        """Persist the task outcome into the team's shared vector memory, so future
        agents recall what was done. Best-effort and gated — never fatal."""
        try:
            from . import agent_memory
            if not agent_memory.is_configured() or result.startswith("[task failed"):
                return
            agent_memory.remember(
                f"Task: {task.get('text','')}\nOutcome: {result[:600]}",
                agent_id=(agent.id if agent else ""),
                agent_name=(agent.name if agent else ""),
                role=(agent.role if agent else task.get("role", "")),
                kind="task")
        except Exception as exc:
            log.warning("memory write skipped: %s", exc)

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
            # Recall relevant team memory and splice it in — the agent acts on what
            # the company has already learned, not from a cold start. (No-op if off.)
            text = task.get("text", "")
            try:
                from . import agent_memory
                mem = agent_memory.recall_block(text, k=4)
                if mem:
                    system += "\n\n" + mem
            except Exception:
                pass
            tools = build_tools_sync(role, aid or None, name)
            msgs = [("system", system), ("human", text)]
            with tag(agent_id=aid, agent_name=name, role=role, kind="task"):
                if tools:
                    return run_tool_loop_sync(
                        llm, msgs, tools, max_steps=role_policy.max_steps(role)).strip()
                # No-tool single-shot: safe to serve from the semantic cache, and to
                # populate it. (Tool loops depend on live state, so they're never cached.)
                from . import semantic_cache
                cached = semantic_cache.lookup(text, model=model)
                if cached is not None:
                    return cached
                out = _text(llm.invoke(msgs)).strip()
                semantic_cache.store(text, out, model=model)
                return out
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

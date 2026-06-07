"""Local always-on worker for schedules, heartbeat checklist entries, and runs."""
from __future__ import annotations

import argparse
import concurrent.futures
import time
from contextlib import contextmanager
from pathlib import Path

from . import config
from .approval_policy import ApprovalRequired, canonical_args
from .autonomous import execute_run
from .scheduling import advance_due, iso_utc, next_interval, utc_now
from .store import AgentStore, JobRunRow


@contextmanager
def _singleton_lock(store: AgentStore):
    """Prevent two local daemons from racing restart recovery.

    SQLite claims are atomic, but startup recovery intentionally marks abandoned
    `running` rows as errors. A second live daemon must not perform that recovery
    against the first daemon's work.
    """
    import fcntl

    lock_path = Path(store.db_path).with_suffix(Path(store.db_path).suffix + ".worker.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"another Company.AI worker is already using {store.db_path}"
            ) from exc
        yield


def scan_due(store: AgentStore) -> int:
    """Create at most one catch-up run per overdue recurring source."""
    now = utc_now()
    stamp = iso_utc(now)
    count = 0
    for job in store.due_jobs(stamp):
        if store.enqueue_run("job", job.id, job.next_run_at,
                             job.agent_id, job.instruction):
            count += 1
        nxt = advance_due(job.schedule_type, job.schedule_value, job.timezone, now)
        store.update_job_due(job.id, iso_utc(nxt) if nxt else job.next_run_at,
                             enabled=nxt is not None)
    for item in store.due_heartbeats(stamp):
        if store.enqueue_run("heartbeat", item.id, item.next_run_at,
                             item.agent_id, item.instruction):
            count += 1
        store.update_heartbeat_due(
            item.id, iso_utc(next_interval(item.interval_seconds, now))
        )
    return count


def execute_claimed(store: AgentStore, run: JobRunRow) -> None:
    store.set_status(run.agent_id, "working")
    try:
        store.finish_run(run.id, execute_run(store, run))
    except ApprovalRequired as exc:
        store.wait_for_approval(
            run.id, exc.tool_name, canonical_args(exc.args),
            exc.fingerprint, exc.action_class,
        )
    except Exception as exc:
        store.fail_run(run.id, f"{type(exc).__name__}: {exc}")
    finally:
        agent = store.get(run.agent_id)
        if agent is not None and agent.status != "fired":
            store.set_status(run.agent_id, "idle")


def _post_task_result(store: AgentStore, task: dict, result: str, agent) -> None:
    """Persist a finished firehose task so it's durable + visible in chat."""
    who = agent.name if agent else f"role:{task.get('role') or '?'}"
    print(f"  [task] {who}: {str(task.get('text',''))[:48]} -> {result[:80]}")
    if agent is not None:
        try:
            store.add_message(agent.id, "ai",
                              f"(task) {task.get('text','')}\n\n{result}")
        except Exception:
            pass
    # Work the CEO fired from the terminal returns to the terminal: append a SHORT
    # summary to the active session (the full result is already in the agent's chat
    # above, so keep the terminal log light — cheap to re-wrap and draw).
    if task.get("source") == "terminal":
        try:
            from .ceo_terminal import CompanyTerminal
            label = who if agent else (task.get("role") or "the team")
            summary = " ".join(str(result).split())
            if len(summary) > 200:
                summary = summary[:200].rstrip() + "…"
            CompanyTerminal(store).append(
                "ai", f"[done · {label}] {str(task.get('text','')).strip()[:80]}\n{summary}")
        except Exception:
            pass


def run_service(store: AgentStore, *, once: bool = False) -> None:
    from . import task_queue

    with _singleton_lock(store):
        interrupted = store.fail_interrupted_runs()
        if interrupted:
            print(f"Marked {interrupted} interrupted run(s) as errors.")
        # The fire-and-forget task firehose: drains the Redis task queue with the
        # same scale cap as the rest of the company. Runs alongside scheduled jobs.
        dispatcher = task_queue.Dispatcher(
            store=store, on_result=lambda t, r, a: _post_task_result(store, t, r, a))
        print(f"Company.AI worker active. Polling every {config.WORKER_POLL_S:g}s "
              f"| task queue: {task_queue.pending()} pending.")
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, config.WORKER_CONCURRENCY),
            thread_name_prefix="company-job",
        )
        active: set[concurrent.futures.Future] = set()
        if not once:
            dispatcher.start()        # background firehose drain for the long-running daemon
        try:
            while True:
                scan_due(store)
                active = {f for f in active if not f.done()}
                while len(active) < max(1, config.WORKER_CONCURRENCY):
                    claimed = store.claim_next_run()
                    if claimed is None:
                        break
                    active.add(pool.submit(execute_claimed, store, claimed))
                if once:
                    n = dispatcher.drain_once()   # also clear any queued firehose tasks
                    if n:
                        print(f"Drained {n} queued task(s).")
                    concurrent.futures.wait(active)
                    return
                time.sleep(config.WORKER_POLL_S)
        except KeyboardInterrupt:
            print("\nstopped.")
        finally:
            dispatcher.stop()
            pool.shutdown(wait=True)


def start_background(db_path: str | None = None):
    """Run the always-on worker in a daemon thread (for in-process use by the game),
    so scheduled 24/7 jobs fire while you play. Returns the Thread, or None if it's
    disabled (COMPANY_AI_WORKER=0) or another worker already holds the db lock.

    Safe to call unconditionally: with no jobs scheduled it just polls cheaply, and
    it never blocks the game — failures (no API key, lock held) are swallowed.
    """
    import os
    import threading

    if os.getenv("COMPANY_AI_WORKER", "1").strip().lower() in ("0", "false", "no", "off"):
        return None

    def _run() -> None:
        try:
            run_service(AgentStore(db_path) if db_path else AgentStore())
        except RuntimeError as exc:        # another worker holds the singleton lock
            print(f"[worker] not started: {exc}")
        except Exception as exc:           # never take the game down with us
            print(f"[worker] stopped: {type(exc).__name__}: {exc}")

    thread = threading.Thread(target=_run, name="company-worker", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    from .observability import init_weave

    init_weave()  # trace scheduled autonomous runs (no-op without WANDB_API_KEY)
    ap = argparse.ArgumentParser(description="Run Company.AI autonomous schedules.")
    ap.add_argument("--db", default=None, help="path to company.db")
    ap.add_argument("--once", action="store_true", help="scan and drain once, then exit")
    args = ap.parse_args(argv)
    run_service(AgentStore(args.db) if args.db else AgentStore(), once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

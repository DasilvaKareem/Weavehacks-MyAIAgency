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


def run_service(store: AgentStore, *, once: bool = False) -> None:
    with _singleton_lock(store):
        interrupted = store.fail_interrupted_runs()
        if interrupted:
            print(f"Marked {interrupted} interrupted run(s) as errors.")
        print(f"Company.AI worker active. Polling every {config.WORKER_POLL_S:g}s.")
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, config.WORKER_CONCURRENCY),
            thread_name_prefix="company-job",
        )
        active: set[concurrent.futures.Future] = set()
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
                    concurrent.futures.wait(active)
                    return
                time.sleep(config.WORKER_POLL_S)
        except KeyboardInterrupt:
            print("\nstopped.")
        finally:
            pool.shutdown(wait=True)


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

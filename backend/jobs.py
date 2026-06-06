"""Headless management CLI for Company.AI always-on jobs."""
from __future__ import annotations

import argparse

from . import config
from .scheduling import initial_due, iso_utc, next_interval, utc_now
from .store import AgentStore


def _need(value: str | None, prompt: str) -> str:
    return value or input(prompt).strip()


def _print_jobs(store: AgentStore) -> None:
    for j in store.list_jobs():
        state = "on" if j.enabled else "off"
        print(f"{j.id}  [{state}] {j.name}  {j.schedule_type}:{j.schedule_value}  "
              f"next {j.next_run_at}  agent {j.agent_id}")


def _print_heartbeats(store: AgentStore) -> None:
    for h in store.list_heartbeats():
        state = "on" if h.enabled else "off"
        print(f"{h.id}  [{state}] {h.name}  every {h.interval_seconds}s  "
              f"next {h.next_run_at}  agent {h.agent_id}")


def _print_runs(store: AgentStore) -> None:
    for r in store.list_runs():
        print(f"{r.id}  [{r.status}] {r.source_type}  {r.agent_name}  {r.created_at}")


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Manage Company.AI autonomous jobs.")
    ap.add_argument("--db", default=None, help="path to company.db")
    subs = ap.add_subparsers(dest="area", required=True)

    jobs = subs.add_parser("jobs")
    jsub = jobs.add_subparsers(dest="command", required=True)
    jsub.add_parser("list")
    for kind in ("once", "interval", "cron"):
        p = jsub.add_parser(f"create-{kind}")
        p.add_argument("--agent")
        p.add_argument("--name")
        p.add_argument("--instruction")
        p.add_argument("--value", help="local ISO time, seconds, or cron expression")
        p.add_argument("--timezone", default=config.DEFAULT_TIMEZONE)
    for cmd in ("enable", "disable", "run-now"):
        p = jsub.add_parser(cmd)
        p.add_argument("id")

    hb = subs.add_parser("heartbeat")
    hsub = hb.add_subparsers(dest="command", required=True)
    hsub.add_parser("list")
    add = hsub.add_parser("add")
    add.add_argument("--agent")
    add.add_argument("--name")
    add.add_argument("--instruction")
    add.add_argument("--seconds", type=int)
    for cmd in ("enable", "disable"):
        p = hsub.add_parser(cmd)
        p.add_argument("id")

    approvals = subs.add_parser("approvals")
    asub = approvals.add_subparsers(dest="command", required=True)
    asub.add_parser("list")
    for cmd in ("approve", "reject"):
        p = asub.add_parser(cmd)
        p.add_argument("id")

    runs = subs.add_parser("runs")
    rsub = runs.add_subparsers(dest="command", required=True)
    rsub.add_parser("list")
    show = rsub.add_parser("show")
    show.add_argument("id")
    retry = rsub.add_parser("retry")
    retry.add_argument("id")

    agents = subs.add_parser("agents")
    gsub = agents.add_subparsers(dest="command", required=True)
    trust = gsub.add_parser("trust")
    trust.add_argument("id")
    trust.add_argument("tier", choices=("supervised", "standard", "trusted"))
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    args = _build_parser().parse_args(argv)
    store = AgentStore(args.db) if args.db else AgentStore()

    if args.area == "jobs":
        if args.command == "list":
            _print_jobs(store)
        elif args.command.startswith("create-"):
            kind = args.command[7:]
            agent = _need(args.agent, "Agent id: ")
            name = _need(args.name, "Job name: ")
            instruction = _need(args.instruction, "Instruction: ")
            value = _need(args.value, "Schedule value: ")
            due = initial_due(kind, value, args.timezone)
            row = store.create_job(agent, name, instruction, kind, value,
                                   args.timezone, iso_utc(due))
            print(f"Created {row.id}; next run {row.next_run_at}")
        elif args.command in {"enable", "disable"}:
            store.set_job_enabled(args.id, args.command == "enable")
        elif args.command == "run-now":
            job = store.get_job(args.id)
            if job is None:
                raise SystemExit(f"No job {args.id}")
            row = store.enqueue_manual_run(job.agent_id, job.instruction, iso_utc(utc_now()))
            print(f"Queued {row.id}" if row else "Agent is unavailable")

    elif args.area == "heartbeat":
        if args.command == "list":
            _print_heartbeats(store)
        elif args.command == "add":
            seconds = args.seconds or int(input("Interval seconds: "))
            row = store.create_heartbeat(
                _need(args.agent, "Agent id: "), _need(args.name, "Checklist name: "),
                _need(args.instruction, "Instruction: "), seconds,
                iso_utc(next_interval(seconds)),
            )
            print(f"Created {row.id}; next run {row.next_run_at}")
        else:
            store.set_heartbeat_enabled(args.id, args.command == "enable")

    elif args.area == "approvals":
        if args.command == "list":
            for a in store.list_approvals():
                print(f"{a.id}  [{a.action_class}] {a.tool_name} {a.tool_args}  run {a.run_id}")
        else:
            store.decide_approval(args.id, "approved" if args.command == "approve"
                                  else "rejected")

    elif args.area == "runs":
        if args.command == "list":
            _print_runs(store)
        elif args.command == "show":
            row = store.get_run(args.id)
            if row is None:
                raise SystemExit(f"No run {args.id}")
            print(row)
        else:
            store.retry_run(args.id)

    elif args.area == "agents":
        store.set_trust_tier(args.id, args.tier)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

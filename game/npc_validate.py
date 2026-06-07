"""Load-time consistency check across the data-driven NPC definitions.

A quest-stop NPC is defined in three places that must agree:
  - assets/park_lots.json  — the NPC entry, its task/tasks key(s), and position
  - game/tasks.py          — the Task with that key (tasks.TASK_BY_KEY)
  - assets/dialogue.json   — the spoken beat for that key (game/dialogue.py)

A typo in any one silently degrades the NPC — a quest that can't complete, or the
generic fallback line instead of the authored scene — with no error anywhere.
This module turns those silent gaps into explicit warnings at startup, and
doubles as a quick standalone check:

    python -m game.npc_validate

It only reads the data files (via the existing loaders), so it's cheap and
side-effect free; the launcher calls report() once before the game starts.
"""
from __future__ import annotations

from . import dialogue, park, tasks


def check() -> list[str]:
    """Cross-check the three NPC data sources; return human-readable warnings
    ([] means everything agrees). Keyed on how the game actually resolves things:
    quest stops look up dialogue by their TASK key (main._open_quest_task), and
    storefronts by their STORE name (main._open_store_greeting)."""
    try:
        npcs = park.load_npc()
    except Exception as exc:                       # malformed/missing park_lots.json
        return [f"could not read NPCs from park_lots.json: {exc}"]
    beats = dialogue.load()                        # {key: Beat}; {} if unreadable
    known = tasks.TASK_BY_KEY                       # {key: Task}
    warns: list[str] = []
    referenced: set[str] = set()                   # every key any NPC points at

    for n in npcs:
        nid = n.get("id", "<no-id>")
        # 1) quest task key(s): a single `task` or a `tasks` workshop list
        for k in ([n["task"]] if n.get("task") else []) + list(n.get("tasks", ())):
            referenced.add(k)
            in_tasks, in_beats = k in known, k in beats
            if not in_tasks and not in_beats:
                warns.append(f"[{nid}] quest key {k!r} is in neither tasks.py nor "
                             f"dialogue.json — walk-up just completes, with no scene")
            elif in_tasks and not in_beats and _asks(known[k]):
                warns.append(f"[{nid}] task {k!r} prompts for input but has no beat "
                             f"in dialogue.json — the NPC shows the generic fallback line")

        # 2) storefront / service greetings are keyed by the store/service name;
        #    record them as referenced so legit greeting beats aren't called orphans.
        for val in (n.get("store"), n.get("service")):
            if val:
                referenced.add(val)

        # 3) position: no grid address AND parked at the origin → renders at (0,0)
        if (nid not in park.NPC_ADDR
                and float(n.get("x", 0)) == 0.0 and float(n.get("z", 0)) == 0.0):
            warns.append(f"[{nid}] no NPC_ADDR entry and x/z are both 0 — "
                         f"this NPC will render at the world origin")

    # 4) authored beats nobody can reach: not a task key, and no NPC points at them
    #    (a renamed task usually leaves the old scene stranded here).
    for k in beats:
        if k not in known and k not in referenced:
            warns.append(f"dialogue.json beat {k!r} matches no task key or NPC "
                         f"reference — orphaned scene (renamed/typo'd key?)")

    return warns


def _asks(task) -> bool:
    """True if a task prompts the player for a typed answer (so a missing beat
    means the fallback line is shown during input, not just skipped)."""
    return bool(getattr(task, "ask", "") and getattr(task, "field", ""))


def report(log=print) -> int:
    """Print any warnings; return the count so a CLI/CI can gate on it. Never
    raises — a broken data file degrades to a single warning, not a crash."""
    warns = check()
    if not warns:
        log("npc check: ok — park_lots.json, tasks.py and dialogue.json agree.")
        return 0
    log(f"npc check: {len(warns)} warning(s) —")
    for w in warns:
        log("  • " + w)
    return len(warns)


if __name__ == "__main__":
    raise SystemExit(1 if report() else 0)

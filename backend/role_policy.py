"""Per-role runtime overrides the optimizer writes and the worker obeys.

This is the WRITE side of the self-optimization loop: the Observability Engineer
reads Weave traces, decides a role is too expensive/slow, and records an override
here (a cheaper model, or a smaller tool-step budget). The worker (agents.py)
consults this on every task, so the next company run is measurably cheaper —
telemetry → decision → behavior change, all data-driven.

Persisted to JSON next to company.db so it survives restarts and the change is
visible/inspectable (good for the demo and for judges reading the repo).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("company.policy")

_cache: dict[str, dict] = {}  # keyed by file path → so each company is isolated


def _path() -> Path:
    """This company's role-policy file, next to its db (per-company isolation)."""
    try:
        from . import workspace
        return Path(workspace.active_db_path()).parent / "role_policy.json"
    except Exception:
        return Path(__file__).resolve().parent.parent / "company_role_policy.json"


def _load() -> dict:
    key = str(_path())
    if key not in _cache:
        try:
            _cache[key] = json.loads(Path(key).read_text())
        except Exception:
            _cache[key] = {}
    return _cache[key]


def _save() -> None:
    try:
        _path().write_text(json.dumps(_load(), indent=2))
    except Exception as exc:  # never let a policy write break a run
        log.warning("could not persist role policy: %s", exc)


def _key(role: str) -> str:
    """Canonical bucket so HR retuning 'Research Analyst' and the graph worker
    running as 'Researcher' (and that agent's 1:1 chats) all hit the same policy."""
    try:
        from .weave_metrics import canon_role
        return canon_role(role)
    except Exception:
        return (role or "").strip().lower()


def get(role: str) -> dict:
    """The override dict for a role, e.g. {'model': ..., 'max_tool_steps': 2}."""
    return dict(_load().get(_key(role), {}))


def model(role: str) -> str | None:
    return get(role).get("model")


def max_steps(role: str) -> int | None:
    return get(role).get("max_tool_steps")


def set(role: str, **overrides) -> dict:
    """Merge overrides for a role (None values are dropped). Returns the new dict."""
    pol = _load()
    cur = dict(pol.get(_key(role), {}))
    for k, v in overrides.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    pol[_key(role)] = cur
    _save()
    log.info("role policy: %s -> %s", role, cur)
    return cur


def replace(role: str, overrides: dict) -> None:
    """Set a role's overrides to EXACTLY `overrides` (used to revert a change that
    hurt quality). Empty dict clears the role."""
    pol = _load()
    if overrides:
        pol[_key(role)] = dict(overrides)
    else:
        pol.pop(_key(role), None)
    _save()


def all() -> dict:
    return dict(_load())


def reset(role: str | None = None) -> None:
    """Clear one role's overrides, or all of them (use to restore the baseline)."""
    pol = _load()
    if role is None:
        pol.clear()
    else:
        pol.pop(_key(role), None)
    _save()

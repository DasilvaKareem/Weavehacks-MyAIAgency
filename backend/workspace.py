"""Per-company workspaces: an isolated, browsable folder per company.

Each company gets `workspaces/<slug>/` with its OWN `company.db` (agents, chats,
drive, schedules — fully isolated), a real `drive/` folder mirroring the shared
drive so its work is human-browsable, an `assets/` folder for generated media, and
a `README.md`. A top-level `registry.json` lists companies and `.active` points at
the current one (overridable per-process with COMPANY_AI_COMPANY).

This module owns NO backend imports (store imports it, not the other way), so it's
safe to resolve very early — `store.AgentStore()` with no args opens the active
company's db, which makes the whole system multi-company in one change.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("company.workspace")

_LEGACY_DB = Path(__file__).resolve().parent.parent / "company.db"


def root_dir() -> Path:
    return Path(os.getenv("COMPANY_AI_WORKSPACES", "")
                or (Path(__file__).resolve().parent.parent / "workspaces"))


def _registry_path() -> Path:
    return root_dir() / "registry.json"


def _active_path() -> Path:
    return root_dir() / ".active"


def _load_registry() -> dict:
    try:
        return json.loads(_registry_path().read_text())
    except Exception:
        return {}


def _save_registry(reg: dict) -> None:
    root_dir().mkdir(parents=True, exist_ok=True)
    _registry_path().write_text(json.dumps(reg, indent=2))


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "company"


# --- per-company paths ------------------------------------------------------

def root(slug: str) -> Path:
    return root_dir() / slug


def db_path(slug: str) -> Path:
    return root(slug) / "company.db"


def drive_dir(slug: str) -> Path:
    return root(slug) / "drive"


def asset_dir(slug: str) -> Path:
    d = root(slug) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_companies() -> dict:
    return _load_registry()


def _readme(slug: str, name: str) -> str:
    return (
        f"# {name}\n\n"
        f"Company workspace — everything this company's AI team produces lives here.\n\n"
        f"- **company.db** — this company's isolated brain (agents, chats, the drive, schedules).\n"
        f"- **drive/** — a browsable mirror of the shared company drive (specs, notes, reports).\n"
        f"- **assets/** — generated images and videos.\n\n"
        f"_slug: `{slug}` · managed by backend/workspace.py · switch with "
        f"`python -m backend.workspace use {slug}`._\n"
    )


def ensure(slug: str, name: str | None = None) -> Path:
    """Create the company's dirs + README (idempotent). Returns its root path."""
    r = root(slug)
    drive_dir(slug).mkdir(parents=True, exist_ok=True)
    asset_dir(slug)
    nm = name or _load_registry().get(slug, {}).get("name") or slug
    try:
        (r / "README.md").write_text(_readme(slug, nm))
    except Exception as exc:  # never fatal
        log.warning("could not write README for %s: %s", slug, exc)
    return r


def create_company(name: str) -> str:
    """Register a new company (unique slug) + scaffold its folder. Returns the slug."""
    reg = _load_registry()
    base = slugify(name)
    slug, i = base, 2
    while slug in reg:
        slug, i = f"{base}-{i}", i + 1
    reg[slug] = {"name": name, "created_at": datetime.now(timezone.utc).isoformat()}
    _save_registry(reg)
    ensure(slug, name)
    log.info("created company '%s' (%s)", name, slug)
    return slug


def set_active(slug: str) -> None:
    root_dir().mkdir(parents=True, exist_ok=True)
    _active_path().write_text(slug)


def _read_legacy_name() -> str | None:
    """Best-effort company name out of the pre-workspace root company.db."""
    try:
        con = sqlite3.connect(str(_LEGACY_DB))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT value FROM settings WHERE key IN ('company_profile','ceo_profile')"
        ).fetchall()
        con.close()
        for row in rows:
            try:
                d = json.loads(row["value"])
                nm = d.get("name") or d.get("company_name")
                if nm:
                    return str(nm).strip()
            except Exception:
                pass
    except Exception:
        pass
    return None


def _bootstrap() -> str:
    """Guarantee at least one company exists; return the active slug. On first run,
    adopt the existing root company.db as the first company (copied, original kept)."""
    reg = _load_registry()
    if not reg:
        if _LEGACY_DB.exists():
            slug = create_company(_read_legacy_name() or "My Company")
            try:
                shutil.copy2(_LEGACY_DB, db_path(slug))  # copy, don't move (safe)
                log.info("adopted existing company.db into %s", slug)
            except Exception as exc:
                log.warning("could not adopt legacy db: %s", exc)
        else:
            slug = create_company("My Company")
        set_active(slug)
        return slug
    slug = ""
    try:
        slug = _active_path().read_text().strip()
    except Exception:
        pass
    if not slug or slug not in reg:
        slug = next(iter(reg))
        set_active(slug)
    return slug


def active_slug() -> str:
    """The current company. COMPANY_AI_COMPANY (slug or name) overrides per-process."""
    env = os.getenv("COMPANY_AI_COMPANY")
    if env:
        reg = _load_registry()
        if env in reg:
            return env
        slug = slugify(env)
        return slug if slug in reg else create_company(env)
    return _bootstrap()


def active_db_path() -> str:
    """Resolve (and scaffold) the active company's db path. This is what
    store.AgentStore() defaults to."""
    slug = active_slug()
    ensure(slug)
    return str(db_path(slug))


# --- CLI: python -m backend.workspace [list | new "Name" | use <slug>] ---------

def main(argv: list[str]) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "new" and len(argv) >= 3:
        slug = create_company(" ".join(argv[2:]))
        set_active(slug)
        print(f"Created and switched to '{slug}'. Folder: {root(slug)}")
    elif cmd == "use" and len(argv) >= 3:
        slug = argv[2]
        if slug not in _load_registry():
            print(f"No company '{slug}'. Known: {', '.join(_load_registry()) or '(none)'}")
            return 1
        set_active(slug)
        print(f"Active company is now '{slug}'.")
    else:  # list
        active = _bootstrap()
        reg = _load_registry()
        print(f"Companies (workspaces/ — active = '{active}'):")
        for slug, meta in reg.items():
            mark = "*" if slug == active else " "
            print(f"  {mark} {slug:24} {meta.get('name','')}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))

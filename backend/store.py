"""Local SQL persistence for hired agents and their conversations.

Uses Python's stdlib `sqlite3` — no extra dependency, one local file
(`company.db` by default). Each hired agent is a row in `agents`; every chat
turn is a row in `messages` keyed by agent id. This is what lets agents survive
a restart and what gives `backend.chat` per-agent memory.

Scale notes: WAL mode is enabled so reads don't block the single writer, and
each connection is short-lived. For the agent counts this game produces, SQLite
is comfortably within range; the schema (id-keyed rows, indexed FKs) ports
cleanly to Postgres later if the company outgrows a local file.
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("company.store")

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "company.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL,
    dept       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'idle',   -- idle | working | done | fired
    model      TEXT,                            -- LLM model (Gemini), not the avatar
    char_model TEXT,                            -- avatar gltf chosen in the marketplace
    char_appearance TEXT,                        -- JSON look (skin/hair/hairstyle/eyes/suit) chosen at hire
    policy     TEXT,                            -- JSON movement policy (game/behavior.py)
    home_room  TEXT,                            -- interior room key this agent works in
    trust_tier TEXT NOT NULL DEFAULT 'supervised', -- supervised | standard | trusted
    hired_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id  TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    role      TEXT NOT NULL,                    -- 'human' (CEO) | 'ai' (agent)
    content   TEXT NOT NULL,
    ts        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent_id, id);

-- Durable record of agent-to-agent meetings. The live channel is Firebase RTDB
-- (backend/meeting_store.py); this is the permanent saved transcript.
CREATE TABLE IF NOT EXISTS meetings (
    id          TEXT PRIMARY KEY,
    topic       TEXT NOT NULL,
    members     TEXT NOT NULL DEFAULT '',     -- comma-separated agent ids
    summary     TEXT,
    status      TEXT NOT NULL DEFAULT 'open',  -- open | closed
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meeting_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id  TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    sender      TEXT NOT NULL,                 -- agent id, or 'ceo'
    name        TEXT NOT NULL,                 -- display name at meeting time
    content     TEXT NOT NULL,
    ts          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_meeting_msgs ON meeting_messages(meeting_id, id);

-- Performance reviews written by the HR agent (backend/hr_tools.py). One row per
-- evaluation, so an agent accrues a history; the latest is its current score. A
-- fire also drops a final row here as the on-record reason.
CREATE TABLE IF NOT EXISTS evaluations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id  TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    reviewer  TEXT NOT NULL DEFAULT 'HR',     -- who reviewed (HR agent name, or 'HR')
    score     INTEGER NOT NULL,               -- 0..100
    summary   TEXT NOT NULL DEFAULT '',       -- written rationale / feedback
    ts        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_eval_agent ON evaluations(agent_id, id);

-- Small key/value store for singletons that aren't agents — e.g. the player's
-- CEO profile (appearance + name) chosen in the first-launch onboarding tutorial.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The company file system: a shared, persistent "drive" every agent can read and
-- write (backend/company_fs.py exposes it as drive_* tools). Files are addressed
-- by a virtual path ('/marketing/brief.md'); `folder` + `name` are split out so a
-- directory listing is one indexed query. Text artifacts live inline in `content`;
-- binary ones (generated images/videos) store an on-disk pointer in `disk_path`.
-- DELIBERATE: author_id has NO foreign key to agents — company files OUTLIVE the
-- agent who made them, so firing someone never cascades away their work.
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,           -- virtual path, e.g. /marketing/brief.md
    name        TEXT NOT NULL,                  -- basename
    folder      TEXT NOT NULL DEFAULT '/',      -- parent dir (for fast listing)
    kind        TEXT NOT NULL DEFAULT 'text',   -- text | image | video | binary
    mime        TEXT,
    content     TEXT,                           -- inline text (NULL for binary refs)
    disk_path   TEXT,                           -- on-disk pointer for binary assets
    size        INTEGER NOT NULL DEFAULT 0,     -- chars (text) or bytes (binary)
    author_id   TEXT,                           -- agent id, or NULL for the CEO/human
    author_name TEXT NOT NULL DEFAULT 'CEO',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder, name);

-- Always-on automation. A schedule produces immutable run records; approvals
-- pause a run before a guarded tool call and survive worker restarts.
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    agent_id         TEXT NOT NULL REFERENCES agents(id),
    name             TEXT NOT NULL,
    instruction      TEXT NOT NULL,
    schedule_type    TEXT NOT NULL,              -- once | interval | cron
    schedule_value   TEXT NOT NULL,              -- UTC timestamp | seconds | cron
    timezone         TEXT NOT NULL DEFAULT 'America/Los_Angeles',
    next_run_at      TEXT NOT NULL,              -- UTC ISO-8601
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS heartbeat_entries (
    id               TEXT PRIMARY KEY,
    agent_id         TEXT NOT NULL REFERENCES agents(id),
    name             TEXT NOT NULL,
    instruction      TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    next_run_at      TEXT NOT NULL,              -- UTC ISO-8601
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_heartbeat_due ON heartbeat_entries(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS job_runs (
    id               TEXT PRIMARY KEY,
    source_type      TEXT NOT NULL,              -- job | heartbeat | manual
    source_id        TEXT,
    scheduled_for    TEXT NOT NULL,              -- UTC ISO-8601
    agent_id         TEXT NOT NULL,
    agent_name       TEXT NOT NULL,
    agent_role       TEXT NOT NULL,
    instruction      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued',
    report           TEXT,
    error            TEXT,
    denial_context   TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_type, source_id, scheduled_for)
);
CREATE INDEX IF NOT EXISTS idx_job_runs_status ON job_runs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_job_runs_agent ON job_runs(agent_id, created_at);

CREATE TABLE IF NOT EXISTS approvals (
    id               TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
    tool_name        TEXT NOT NULL,
    tool_args        TEXT NOT NULL,              -- canonical JSON
    fingerprint      TEXT NOT NULL,
    action_class     TEXT NOT NULL,
    decision         TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
    consumed         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_pending ON approvals(decision, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_one_pending
    ON approvals(run_id, fingerprint) WHERE decision='pending';
"""


@dataclass
class AgentRow:
    id: str
    name: str
    role: str
    dept: str
    status: str
    model: str | None
    hired_at: str
    char_model: str | None = None    # avatar gltf; default keeps old callers working
    char_appearance: str | None = None  # JSON look chosen at hire (skin/hair/eyes/suit)
    policy: str | None = None        # JSON movement policy; None until the planner runs
    home_room: str | None = None     # interior room key (which wing the agent works in)
    trust_tier: str = "supervised"


@dataclass
class Message:
    role: str       # 'human' or 'ai'
    content: str
    ts: str


@dataclass
class MeetingRow:
    id: str
    topic: str
    members: str
    summary: str | None
    status: str
    created_at: str


@dataclass
class MeetingLine:
    sender: str
    name: str
    content: str
    ts: str


@dataclass
class EvaluationRow:
    id: int
    agent_id: str
    reviewer: str
    score: int
    summary: str
    ts: str


@dataclass
class FileRow:
    id: int
    path: str
    name: str
    folder: str
    kind: str
    mime: str | None
    content: str | None
    disk_path: str | None
    size: int
    author_id: str | None
    author_name: str
    created_at: str
    updated_at: str


@dataclass
class JobRow:
    id: str
    agent_id: str
    name: str
    instruction: str
    schedule_type: str
    schedule_value: str
    timezone: str
    next_run_at: str
    enabled: int
    created_at: str
    updated_at: str


@dataclass
class HeartbeatRow:
    id: str
    agent_id: str
    name: str
    instruction: str
    interval_seconds: int
    next_run_at: str
    enabled: int
    created_at: str
    updated_at: str


@dataclass
class JobRunRow:
    id: str
    source_type: str
    source_id: str | None
    scheduled_for: str
    agent_id: str
    agent_name: str
    agent_role: str
    instruction: str
    status: str
    report: str | None
    error: str | None
    denial_context: str | None
    started_at: str | None
    finished_at: str | None
    created_at: str


@dataclass
class ApprovalRow:
    id: str
    run_id: str
    tool_name: str
    tool_args: str
    fingerprint: str
    action_class: str
    decision: str
    consumed: int
    created_at: str
    decided_at: str | None


class AgentStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        # No explicit path → open the ACTIVE company's db (per-company workspace).
        # Explicit path still honored (tests, --db flags, sub-stores).
        if db_path is None:
            from . import workspace
            db_path = workspace.active_db_path()
        self.db_path = str(db_path)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(conn) -> None:
        # Add columns introduced after a DB was first created (e.g. `dept`).
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)")}
        if "dept" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN dept TEXT NOT NULL DEFAULT ''")
        if "char_model" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN char_model TEXT")
        if "char_appearance" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN char_appearance TEXT")
        if "policy" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN policy TEXT")
        if "home_room" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN home_room TEXT")
        if "trust_tier" not in cols:
            conn.execute(
                "ALTER TABLE agents ADD COLUMN trust_tier TEXT NOT NULL DEFAULT 'supervised'"
            )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- agents ------------------------------------------------------------

    def hire(self, name: str, role: str, dept: str = "", model: str | None = None,
             agent_id: str | None = None, char_model: str | None = None,
             char_appearance: str | None = None) -> AgentRow:
        """Persist a newly hired agent and return its row."""
        aid = agent_id or uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT INTO agents (id, name, role, dept, model, char_model, char_appearance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (aid, name, role, dept, model, char_model, char_appearance),
            )
        return self.get(aid)  # type: ignore[return-value]

    def get(self, agent_id: str) -> AgentRow | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return AgentRow(**row) if row else None

    def list_agents(self, include_fired: bool = False) -> list[AgentRow]:
        sql = "SELECT * FROM agents"
        if not include_fired:
            sql += " WHERE status != 'fired'"
        sql += " ORDER BY hired_at"
        with self._conn() as c:
            return [AgentRow(**r) for r in c.execute(sql).fetchall()]

    def set_status(self, agent_id: str, status: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE agents SET status = ? WHERE id = ?", (status, agent_id))

    def set_policy(self, agent_id: str, policy_json: str) -> None:
        """Persist a bot's movement policy (JSON) so it's stable across sessions."""
        with self._conn() as c:
            c.execute("UPDATE agents SET policy = ? WHERE id = ?", (policy_json, agent_id))

    def set_role(self, agent_id: str, role: str, dept: str | None = None) -> None:
        """Repurpose an agent: change its role (and optionally department)."""
        with self._conn() as c:
            if dept is None:
                c.execute("UPDATE agents SET role = ? WHERE id = ?", (role, agent_id))
            else:
                c.execute("UPDATE agents SET role = ?, dept = ? WHERE id = ?",
                          (role, dept, agent_id))

    def set_home_room(self, agent_id: str, home_room: str) -> None:
        """Persist which interior room (wing) an agent works in."""
        with self._conn() as c:
            c.execute("UPDATE agents SET home_room = ? WHERE id = ?", (home_room, agent_id))

    def set_trust_tier(self, agent_id: str, tier: str) -> None:
        if tier not in {"supervised", "standard", "trusted"}:
            raise ValueError(f"invalid trust tier: {tier}")
        with self._conn() as c:
            c.execute("UPDATE agents SET trust_tier = ? WHERE id = ?", (tier, agent_id))

    def fire(self, agent_id: str) -> None:
        self.set_status(agent_id, "fired")

    # --- settings (key/value singletons) -----------------------------------

    def get_setting(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- conversation ------------------------------------------------------

    def add_message(self, agent_id: str, role: str, content: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO messages (agent_id, role, content) VALUES (?, ?, ?)",
                (agent_id, role, content),
            )

    def history(self, agent_id: str, limit: int | None = None) -> list[Message]:
        sql = "SELECT role, content, ts FROM messages WHERE agent_id = ? ORDER BY id"
        params: tuple = (agent_id,)
        if limit is not None:
            # newest N, returned oldest-first for prompt ordering
            sql = (
                "SELECT role, content, ts FROM ("
                "SELECT id, role, content, ts FROM messages WHERE agent_id = ? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id"
            )
            params = (agent_id, limit)
        with self._conn() as c:
            return [Message(**r) for r in c.execute(sql, params).fetchall()]

    # --- meetings (durable transcript) -------------------------------------

    def create_meeting_record(self, meeting_id: str, topic: str,
                              members: list[str]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO meetings (id, topic, members) VALUES (?, ?, ?)",
                (meeting_id, topic, ",".join(members)),
            )

    def add_meeting_message(self, meeting_id: str, sender: str, name: str,
                            content: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO meeting_messages (meeting_id, sender, name, content) "
                "VALUES (?, ?, ?, ?)",
                (meeting_id, sender, name, content),
            )

    def finish_meeting(self, meeting_id: str, summary: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE meetings SET summary = ?, status = 'closed' WHERE id = ?",
                      (summary, meeting_id))

    def list_meetings(self) -> list[MeetingRow]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM meetings ORDER BY created_at DESC").fetchall()
        return [MeetingRow(**r) for r in rows]

    def get_meeting(self, meeting_id: str) -> MeetingRow | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return MeetingRow(**r) if r else None

    def meeting_transcript(self, meeting_id: str) -> list[MeetingLine]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT sender, name, content, ts FROM meeting_messages "
                "WHERE meeting_id = ? ORDER BY id", (meeting_id,)
            ).fetchall()
        return [MeetingLine(**r) for r in rows]

    # --- performance evaluations (HR) --------------------------------------

    def add_evaluation(self, agent_id: str, score: int, summary: str = "",
                       reviewer: str = "HR") -> None:
        """Record one performance review for an agent (score clamped to 0..100)."""
        score = max(0, min(100, int(score)))
        with self._conn() as c:
            c.execute(
                "INSERT INTO evaluations (agent_id, reviewer, score, summary) "
                "VALUES (?, ?, ?, ?)",
                (agent_id, reviewer, score, summary),
            )

    def list_evaluations(self, agent_id: str, limit: int | None = None
                         ) -> list[EvaluationRow]:
        """An agent's reviews, newest first."""
        sql = ("SELECT id, agent_id, reviewer, score, summary, ts FROM evaluations "
               "WHERE agent_id = ? ORDER BY id DESC")
        params: tuple = (agent_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (agent_id, limit)
        with self._conn() as c:
            return [EvaluationRow(**r) for r in c.execute(sql, params).fetchall()]

    def latest_evaluation(self, agent_id: str) -> EvaluationRow | None:
        evals = self.list_evaluations(agent_id, limit=1)
        return evals[0] if evals else None

    # --- company file system (the shared drive) ----------------------------

    @staticmethod
    def _norm_path(path: str) -> str:
        """Normalize a virtual path to a single leading slash, no trailing slash."""
        p = "/" + (path or "").strip().strip("/")
        while "//" in p:
            p = p.replace("//", "/")
        return p or "/"

    @classmethod
    def _split_path(cls, path: str) -> tuple[str, str, str]:
        """Return (normalized_path, folder, name) for a virtual path."""
        norm = cls._norm_path(path)
        folder, _, name = norm.rpartition("/")
        return norm, (folder or "/"), name

    def fs_write(self, path: str, content: str, author_id: str | None = None,
                 author_name: str = "CEO", kind: str = "text",
                 mime: str | None = None) -> FileRow:
        """Create or overwrite a text file at a virtual path; return its row.

        Upsert by path: re-writing keeps the original `created_at` but bumps
        `updated_at`, so a file has a stable history while reflecting edits.
        """
        norm, folder, name = self._split_path(path)
        if not name:
            raise ValueError(f"path {path!r} has no file name")
        size = len(content or "")
        with self._conn() as c:
            c.execute(
                "INSERT INTO files (path, name, folder, kind, mime, content, size,"
                " author_id, author_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET content=excluded.content, "
                "kind=excluded.kind, mime=excluded.mime, size=excluded.size, "
                "author_id=excluded.author_id, author_name=excluded.author_name, "
                "disk_path=NULL, updated_at=datetime('now')",
                (norm, name, folder, kind, mime, content, size, author_id, author_name),
            )
        self._mirror_to_disk(norm, content)
        return self.fs_get(norm)  # type: ignore[return-value]

    def _mirror_to_disk(self, norm_path: str, content: str) -> None:
        """Write a drive text file out to this company's browsable drive/ folder,
        next to its db (workspaces/<slug>/drive/<path>). Best-effort, never fatal."""
        try:
            dest = Path(self.db_path).parent / "drive" / norm_path.lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content or "")
        except Exception as exc:  # pragma: no cover - mirror is a convenience
            log.debug("drive mirror skipped for %s: %s", norm_path, exc)

    def fs_attach(self, path: str, disk_path: str, kind: str = "binary",
                  author_id: str | None = None, author_name: str = "CEO",
                  mime: str | None = None) -> FileRow:
        """Register an existing on-disk asset (image/video) into the drive by
        reference, so generated artifacts are catalogued without copying bytes."""
        norm, folder, name = self._split_path(path)
        if not name:
            raise ValueError(f"path {path!r} has no file name")
        try:
            size = Path(disk_path).stat().st_size
        except OSError:
            size = 0
        with self._conn() as c:
            c.execute(
                "INSERT INTO files (path, name, folder, kind, mime, disk_path, size,"
                " author_id, author_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET disk_path=excluded.disk_path, "
                "kind=excluded.kind, mime=excluded.mime, size=excluded.size, "
                "author_id=excluded.author_id, author_name=excluded.author_name, "
                "content=NULL, updated_at=datetime('now')",
                (norm, name, folder, kind, mime, disk_path, size, author_id, author_name),
            )
        return self.fs_get(norm)  # type: ignore[return-value]

    def fs_get(self, path: str) -> FileRow | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM files WHERE path = ?",
                            (self._norm_path(path),)).fetchone()
        return FileRow(**row) if row else None

    def fs_list(self, folder: str | None = None) -> list[FileRow]:
        """List files. With `folder`, only that directory's immediate files;
        otherwise the whole drive, ordered by path."""
        with self._conn() as c:
            if folder is None:
                rows = c.execute("SELECT * FROM files ORDER BY path").fetchall()
            else:
                rows = c.execute("SELECT * FROM files WHERE folder = ? ORDER BY name",
                                 (self._norm_path(folder),)).fetchall()
        return [FileRow(**r) for r in rows]

    def fs_search(self, query: str, limit: int = 50) -> list[FileRow]:
        """Find files whose path/name OR text content matches a substring."""
        like = f"%{query}%"
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM files WHERE path LIKE ? OR content LIKE ? "
                "ORDER BY updated_at DESC LIMIT ?", (like, like, limit)
            ).fetchall()
        return [FileRow(**r) for r in rows]

    def fs_delete(self, path: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM files WHERE path = ?",
                            (self._norm_path(path),))
        return cur.rowcount > 0

    def fs_move(self, src: str, dst: str) -> bool:
        """Rename/move a file to a new virtual path. Returns False if src is
        missing or dst is already taken."""
        s = self._norm_path(src)
        dnorm, dfolder, dname = self._split_path(dst)
        if not dname:
            return False
        with self._conn() as c:
            if c.execute("SELECT 1 FROM files WHERE path = ?", (dnorm,)).fetchone():
                return False
            cur = c.execute(
                "UPDATE files SET path=?, folder=?, name=?, updated_at=datetime('now') "
                "WHERE path=?", (dnorm, dfolder, dname, s)
            )
        return cur.rowcount > 0

    # --- always-on jobs ---------------------------------------------------

    def create_job(self, agent_id: str, name: str, instruction: str,
                   schedule_type: str, schedule_value: str, timezone: str,
                   next_run_at: str, job_id: str | None = None) -> JobRow:
        if schedule_type not in {"once", "interval", "cron"}:
            raise ValueError(f"invalid schedule type: {schedule_type}")
        jid = job_id or uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT INTO jobs (id, agent_id, name, instruction, schedule_type, "
                "schedule_value, timezone, next_run_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (jid, agent_id, name, instruction, schedule_type, schedule_value,
                 timezone, next_run_at),
            )
        return self.get_job(jid)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> JobRow | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return JobRow(**row) if row else None

    def list_jobs(self) -> list[JobRow]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [JobRow(**r) for r in rows]

    def set_job_enabled(self, job_id: str, enabled: bool) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET enabled=?, updated_at=datetime('now') WHERE id=?",
                (int(enabled), job_id),
            )

    def update_job_due(self, job_id: str, next_run_at: str,
                       enabled: bool = True) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET next_run_at=?, enabled=?, updated_at=datetime('now') "
                "WHERE id=?", (next_run_at, int(enabled), job_id)
            )

    def due_jobs(self, now_utc: str) -> list[JobRow]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE enabled=1 AND next_run_at <= ? "
                "ORDER BY next_run_at", (now_utc,)
            ).fetchall()
        return [JobRow(**r) for r in rows]

    def create_heartbeat(self, agent_id: str, name: str, instruction: str,
                         interval_seconds: int, next_run_at: str,
                         entry_id: str | None = None) -> HeartbeatRow:
        if interval_seconds < 60:
            raise ValueError("heartbeat interval must be at least 60 seconds")
        hid = entry_id or uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT INTO heartbeat_entries (id, agent_id, name, instruction, "
                "interval_seconds, next_run_at) VALUES (?, ?, ?, ?, ?, ?)",
                (hid, agent_id, name, instruction, interval_seconds, next_run_at),
            )
        return self.get_heartbeat(hid)  # type: ignore[return-value]

    def get_heartbeat(self, entry_id: str) -> HeartbeatRow | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM heartbeat_entries WHERE id = ?", (entry_id,)
            ).fetchone()
        return HeartbeatRow(**row) if row else None

    def list_heartbeats(self) -> list[HeartbeatRow]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM heartbeat_entries ORDER BY created_at DESC"
            ).fetchall()
        return [HeartbeatRow(**r) for r in rows]

    def set_heartbeat_enabled(self, entry_id: str, enabled: bool) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE heartbeat_entries SET enabled=?, updated_at=datetime('now') "
                "WHERE id=?", (int(enabled), entry_id)
            )

    def update_heartbeat_due(self, entry_id: str, next_run_at: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE heartbeat_entries SET next_run_at=?, updated_at=datetime('now') "
                "WHERE id=?", (next_run_at, entry_id)
            )

    def due_heartbeats(self, now_utc: str) -> list[HeartbeatRow]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM heartbeat_entries WHERE enabled=1 AND next_run_at <= ? "
                "ORDER BY next_run_at", (now_utc,)
            ).fetchall()
        return [HeartbeatRow(**r) for r in rows]

    def enqueue_run(self, source_type: str, source_id: str | None, scheduled_for: str,
                    agent_id: str, instruction: str) -> JobRunRow | None:
        agent = self.get(agent_id)
        if agent is None or agent.status == "fired":
            return None
        rid = uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO job_runs (id, source_type, source_id, "
                "scheduled_for, agent_id, agent_name, agent_role, instruction) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, source_type, source_id, scheduled_for, agent.id, agent.name,
                 agent.role, instruction),
            )
            row = c.execute(
                "SELECT * FROM job_runs WHERE source_type=? AND source_id IS ? "
                "AND scheduled_for=?",
                (source_type, source_id, scheduled_for),
            ).fetchone()
        return JobRunRow(**row) if row else None

    def enqueue_manual_run(self, agent_id: str, instruction: str,
                           scheduled_for: str) -> JobRunRow | None:
        return self.enqueue_run("manual", uuid.uuid4().hex[:12], scheduled_for,
                                agent_id, instruction)

    def get_run(self, run_id: str) -> JobRunRow | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
        return JobRunRow(**row) if row else None

    def list_runs(self, limit: int = 100, agent_id: str | None = None) -> list[JobRunRow]:
        sql = "SELECT * FROM job_runs"
        params: tuple = ()
        if agent_id:
            sql += " WHERE agent_id=?"
            params = (agent_id,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params += (limit,)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [JobRunRow(**r) for r in rows]

    def claim_next_run(self) -> JobRunRow | None:
        """Atomically claim one queued run across competing local workers."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM job_runs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = c.execute(
                "UPDATE job_runs SET status='running', started_at=datetime('now'), "
                "error=NULL WHERE id=? AND status='queued'", (row["id"],)
            )
            if cur.rowcount != 1:
                return None
            row = c.execute("SELECT * FROM job_runs WHERE id=?", (row["id"],)).fetchone()
        return JobRunRow(**row) if row else None

    def finish_run(self, run_id: str, report: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE job_runs SET status='done', report=?, error=NULL, "
                "finished_at=datetime('now') WHERE id=?", (report, run_id)
            )

    def fail_run(self, run_id: str, error: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE job_runs SET status='error', error=?, "
                "finished_at=datetime('now') WHERE id=?", (error, run_id)
            )

    def wait_for_approval(self, run_id: str, tool_name: str, tool_args: str,
                          fingerprint: str, action_class: str) -> ApprovalRow:
        aid = uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO approvals (id, run_id, tool_name, tool_args, "
                "fingerprint, action_class) VALUES (?, ?, ?, ?, ?, ?)",
                (aid, run_id, tool_name, tool_args, fingerprint, action_class),
            )
            c.execute(
                "UPDATE job_runs SET status='waiting_approval' WHERE id=?", (run_id,)
            )
            row = c.execute(
                "SELECT * FROM approvals WHERE run_id=? AND fingerprint=? "
                "AND decision='pending'", (run_id, fingerprint)
            ).fetchone()
        return ApprovalRow(**row)

    def list_approvals(self, decision: str | None = "pending",
                       limit: int = 100) -> list[ApprovalRow]:
        sql = "SELECT * FROM approvals"
        params: tuple = ()
        if decision:
            sql += " WHERE decision=?"
            params = (decision,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params += (limit,)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [ApprovalRow(**r) for r in rows]

    def decide_approval(self, approval_id: str, decision: str) -> None:
        if decision not in {"approved", "rejected"}:
            raise ValueError(f"invalid approval decision: {decision}")
        with self._conn() as c:
            row = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None or row["decision"] != "pending":
                raise ValueError("approval is missing or already decided")
            denial = None
            if decision == "rejected":
                denial = f"CEO rejected {row['tool_name']} with args {row['tool_args']}."
            c.execute(
                "UPDATE approvals SET decision=?, decided_at=datetime('now') WHERE id=?",
                (decision, approval_id),
            )
            c.execute(
                "UPDATE job_runs SET status='queued', denial_context=?, "
                "finished_at=NULL WHERE id=?", (denial, row["run_id"])
            )

    def consume_grant(self, run_id: str, fingerprint: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE approvals SET consumed=1 WHERE id=("
                "SELECT id FROM approvals WHERE run_id=? AND fingerprint=? "
                "AND decision='approved' AND consumed=0 ORDER BY decided_at LIMIT 1"
                ")", (run_id, fingerprint)
            )
        return cur.rowcount == 1

    def retry_run(self, run_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE job_runs SET status='queued', error=NULL, report=NULL, "
                "finished_at=NULL WHERE id=? AND status IN ('error','done')", (run_id,)
            )

    def fail_interrupted_runs(self) -> int:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE job_runs SET status='error', error='worker restarted during run', "
                "finished_at=datetime('now') WHERE status='running'"
            )
        return cur.rowcount

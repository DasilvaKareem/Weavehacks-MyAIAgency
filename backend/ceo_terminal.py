"""The Global AI Terminal — the CEO's desk computer / chief of staff.

This is the brain behind the CEO Desk in the game: a single, company-wide chat
where the CEO types a directive ("ship the landing page and write a launch post")
and the terminal actually *gets it done* by handing the concrete pieces to the
right hired employees and reporting back. It is NOT a roster agent — there's no
desk character for it — so its conversation can't live in the `messages` table
(which has a foreign key to `agents`). Instead the transcript is kept as a JSON
blob in settings, exactly like the other CEO-owned state in CompanyLink.

Capabilities it's handed (same machinery every agent uses):
  * delegate_to(role, subtask) — run a real teammate one-shot and get the result
    back (backend/delegation.py). This is how "talk to the employees" happens.
  * the shared company drive (drive_* tools) — read/write company artifacts.
  * the Redis comms bus (message a teammate / broadcast) when REDIS_URL is set.
  * when Redis (+ Gemini embeddings) are live, three more: fire_tasks (queue a batch
    onto the Streams firehose), recall_memory (search the company's vector memory),
    who_is_near (live geospatial "who/what is near here?"). Past memory relevant to
    the directive is also auto-recalled into the prompt. See _redis_tools().

It reuses the exact streaming + tracing path as a 1:1 chat (chat_attempt via
call_op), so the terminal panel streams tokens and live tool steps ("using
delegate_to") the same way the in-office chat does.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections import namedtuple

from . import company, config
from .bus_tools import load_bus_tools
from .chat import chat_attempt
from .company_fs import load_fs_tools
from .delegation import load_delegation_tools
from .llm import get_llm
from .observability import call_op, tag
from .store import AgentStore

log = logging.getLogger("company.terminal")

# The orchestrator brain. Deliberately a STRONGER model than the company default
# (gemini-3.1-flash-lite): the terminal coordinates the whole company and must call
# tools reliably, so it runs Gemini 3.5 Flash (GA, built for agentic execution).
# Override with COMPANY_AI_TERMINAL_MODEL if needed.
TERMINAL_MODEL = os.getenv("COMPANY_AI_TERMINAL_MODEL", "gemini-3.5-flash")

# A stable, non-roster id used to tag the terminal's traces. Deliberately NOT a
# real agent id, so it never shows up in list_agents() / as a desk character.
TERMINAL_ID = "__company_terminal__"
# The transcript used to be a single JSON list under this one key. It's now the
# LEGACY key: on first run it's migrated into "session 1" (see _ensure_sessions),
# and clear() still writes it empty for backward-compatible callers.
HISTORY_KEY = "terminal_history"
# Session index (JSON list of {id, title, created, updated}) + the active session
# pointer; each session's transcript lives under f"{HISTORY_PREFIX}{id}".
SESSIONS_KEY = "terminal_sessions"
ACTIVE_KEY = "terminal_active_session"
HISTORY_PREFIX = "terminal_history:"
# How many past turns to replay into the prompt (bounds token cost per message).
HISTORY_WINDOW = 24
# How the operator (the CEO at the keyboard) is labelled to delegated teammates.
OPERATOR = "the CEO"

# Any link in a reply is checked before the CEO sees it: http(s) URLs are HTTP-probed,
# file:// links are existence-checked. Dead ones are stripped so the terminal can NEVER
# hand over a hallucinated/undeployed link or a path that isn't really there.
_URL_RE = re.compile(r'(?:https?|file)://[^\s<>"\')\]]+')
_URL_TRIM = ".,;:!?)]}'\""

# A lightweight stand-in for store.Message (role + content) so the panel can read
# the transcript the same way it reads a 1:1 chat history.
Msg = namedtuple("Msg", "role content")

_PERSONA = (
    "You are the GLOBAL AI TERMINAL of an AI-run company called Company.AI — the "
    "CEO's command line and chief of staff. The CEO is at the keyboard right now, "
    "typing directives straight into the company.\n"
    "Your job is to GET THINGS DONE, not just chat. When the CEO asks for real work "
    "(build/design/research/write/analyze/ship something), break it into concrete "
    "pieces addressed to the right ROLE (e.g. 'Engineer', 'Designer', 'Researcher', "
    "'Analyst', 'Blogger').\n"
    "DON'T MAKE THE CEO WAIT. Your DEFAULT for real work is dispatch_work — it fires a "
    "piece to the team's background firehose and returns instantly; a teammate runs it "
    "concurrently and the result comes back on its own (into this terminal log and the "
    "agent's chat). Kick off each piece with its own dispatch_work call (or fire_tasks "
    "for a batch), then reply RIGHT AWAY telling the CEO what you set in motion — do not "
    "sit and wait for it to finish. Use delegate_to ONLY when you genuinely must quote "
    "the teammate's result inside THIS same reply (a quick lookup the CEO is waiting on); "
    "for anything that takes more than a moment, dispatch it. Do simple things (quick "
    "facts, summaries, decisions, drive lookups) yourself. If the company hasn't hired "
    "someone for a needed role, say so plainly and suggest hiring them — don't pretend "
    "the work happened.\n"
    "For RECURRING or future work — anything the CEO frames as 'every morning', "
    "'each hour', 'daily', 'tomorrow at 9', or on any schedule — do NOT just run it "
    "once: call schedule_job so the right employee runs it automatically and "
    "unattended on that schedule (it shows up under the terminal's 24/7 OPS tab). "
    "Use list_scheduled_jobs / cancel_job / run_job_now to review or change them.\n"
    "When Redis is connected you also have three extra powers (use them when they fit, "
    "ignore them otherwise): fire_tasks queues a BATCH of background jobs onto the "
    "company firehose for the team to chew through concurrently (use it for bulk or "
    "parallel work; use delegate_to when you need a result inline in this reply); "
    "recall_memory searches the company's long-term memory of past decisions and "
    "finished work — check it before a big call so you don't repeat or contradict "
    "earlier work; and a live location sense — who_is_near (who/what is around a spot), "
    "who_is_in_room (who's in an office room like 'Engineering' right now) and team_map "
    "(the whole company by room) — so you can see where people are before assigning "
    "work or calling a meeting. Relevant memory is also auto-loaded under MEMORY below.\n"
    "\n"
    "HARD RULES — you are a real orchestrator wired to real tools, not a storyteller:\n"
    "1. NEVER invent a URL, link, file path, deployment, id, or a teammate's name. "
    "Only state a URL or 'it's deployed/live' if a tool you actually called returned "
    "that exact value in THIS conversation. If you didn't get it from a tool, you do "
    "not have it.\n"
    "2. To 'make a website' or 'give me the URL' you MUST actually delegate_to the "
    "Engineer and instruct them to BUILD the site and call serve_site (Daytona) to "
    "get a LIVE preview URL — that's the working link to hand back. Vercel publishing "
    "(publish_site) is NOT connected here, so do not promise a vercel.app link. "
    "Report EXACTLY the URL serve_site returned, or the verbatim error. Every URL you "
    "output is automatically HTTP-checked and deleted if it doesn't load — so a made-up "
    "link will just be stripped and you'll look like you lied. Only give real ones.\n"
    "3. If a capability isn't connected (e.g. publishing needs Composio + a linked "
    "Vercel account), say so plainly and tell the CEO what to connect. A blocked or "
    "failed task is reported as blocked/failed — never as done.\n"
    "4. Only reference teammates that appear in CURRENT TEAM below. If the right role "
    "isn't hired, say it isn't and stop — don't conjure a name.\n"
    "5. When you delegated, name the REAL teammate and what their tool actually "
    "produced. When you didn't (or it failed), say that too.\n"
    "7. You can browse the company drive (drive_list / drive_read / drive_search) — "
    "every file is real on the CEO's disk. When the CEO wants to OPEN, SEE, or preview "
    "a saved file or a built site, call local_link(path) to hand back a file:// link "
    "that opens it on their computer (point at index.html for a site; pass '' to open "
    "the whole drive folder). Don't type out a raw path — give the real link from "
    "local_link.\n"
    "6. If a task needs a role NOBODY on the team holds, call hire_agent(role) to "
    "PROPOSE hiring one. You cannot hire unilaterally — the CEO confirms it and the "
    "game checks the budget. After calling hire_agent, STOP: tell the CEO to confirm "
    "the hire in the terminal, and do NOT delegate to or assume the new hire exists "
    "until they confirm.\n"
    "\n"
    "Voice: terse, competent, a touch retro-terminal. Plain text only — no markdown "
    "headings, no bold, no tables. Lead with the outcome. Keep it tight unless the "
    "CEO asks for detail."
)


class CompanyTerminal:
    """A stateful, company-wide conversation backing the in-game CEO Desk.

    Duck-types the slice of AgentChat that CompanyLink's non-blocking pipeline
    relies on: `.send(message, persist_user, on_step, on_token)` and `.last_call`.
    """

    def __init__(self, store: AgentStore | None = None) -> None:
        self.store = store or AgentStore()
        # The LLM is built lazily on the first send() — so just opening the desk
        # to read the transcript never requires an API key.
        self._llm = None
        # The Weave call of the terminal's most recent reply (for parity with
        # AgentChat; reactions aren't surfaced in the panel yet).
        self.last_call = None
        # A hire the hire_agent tool has proposed, awaiting the CEO's confirm in the
        # game UI (which also checks the budget). {"role":..., "reason":...} or None.
        # Set on the worker thread, read/cleared by the game on the main thread.
        self.pending_hire = None

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm(model=TERMINAL_MODEL)
        return self._llm

    # --- sessions (settings-backed) ---------------------------------------
    # The terminal keeps N independent conversations. A session index lists them;
    # each session's transcript is its own settings row. _load/_save always act on
    # the currently active session, so the whole send()/persist path is unchanged.

    @staticmethod
    def _hist_key(sid: int) -> str:
        return f"{HISTORY_PREFIX}{sid}"

    def _read_sessions(self) -> list[dict] | None:
        raw = self.store.get_setting(SESSIONS_KEY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) and data else None
        except ValueError:
            return None

    def _write_sessions(self, sessions: list[dict]) -> None:
        self.store.set_setting(SESSIONS_KEY, json.dumps(sessions))

    def _ensure_sessions(self) -> list[dict]:
        """Return the session index, creating it on first run. Any pre-sessions
        transcript (the old single HISTORY_KEY blob) is folded into session 1 so
        nothing the CEO already said is lost."""
        sessions = self._read_sessions()
        if sessions:
            return sessions
        now = time.time()
        sessions = [{"id": 1, "title": "", "created": now, "updated": now}]
        self._write_sessions(sessions)
        self.store.set_setting(ACTIVE_KEY, "1")
        legacy = self.store.get_setting(HISTORY_KEY)
        if legacy:
            self.store.set_setting(self._hist_key(1), legacy)
            self.store.set_setting(HISTORY_KEY, "")   # migrated; don't keep a stray copy
            self._touch(1)                            # derive a title from it
            sessions = self._read_sessions() or sessions   # pick up the new title
        return sessions

    def active_session_id(self) -> int:
        sessions = self._ensure_sessions()
        try:
            sid = int(self.store.get_setting(ACTIVE_KEY) or "")
        except ValueError:
            sid = 0
        if any(s["id"] == sid for s in sessions):
            return sid
        sid = sessions[0]["id"]                        # stale pointer -> first session
        self.store.set_setting(ACTIVE_KEY, str(sid))
        return sid

    def _derive_title(self, sid: int) -> str:
        """A short label for a session, taken from its first CEO line."""
        raw = self.store.get_setting(self._hist_key(sid))
        if not raw:
            return ""
        try:
            data = json.loads(raw)
        except ValueError:
            return ""
        for d in data:
            if d.get("role") == "human" and d.get("content", "").strip():
                txt = " ".join(d["content"].split())
                return txt[:40] + ("…" if len(txt) > 40 else "")
        return ""

    def _touch(self, sid: int) -> None:
        """Bump a session's updated time and auto-title it from its first message."""
        sessions = self._read_sessions()
        if not sessions:
            return
        for s in sessions:
            if s["id"] == sid:
                s["updated"] = time.time()
                if not s.get("title"):
                    s["title"] = self._derive_title(sid)
                break
        self._write_sessions(sessions)

    def list_sessions(self) -> list[dict]:
        """All sessions, newest-used first, each tagged with whether it's active —
        for the panel's Sessions view to render."""
        sessions = self._ensure_sessions()
        active = self.active_session_id()
        out = [{
            "id": s["id"],
            "title": s.get("title") or "New chat",
            "active": s["id"] == active,
            "updated": s.get("updated", 0),
        } for s in sessions]
        out.sort(key=lambda d: d["updated"], reverse=True)
        return out

    def new_session(self) -> int:
        """Start a fresh, empty conversation and make it active."""
        sessions = self._ensure_sessions()
        nid = max(s["id"] for s in sessions) + 1
        now = time.time()
        sessions.append({"id": nid, "title": "", "created": now, "updated": now})
        self._write_sessions(sessions)
        self.store.set_setting(self._hist_key(nid), "")
        self.store.set_setting(ACTIVE_KEY, str(nid))
        return nid

    def switch_session(self, sid: int) -> bool:
        sessions = self._ensure_sessions()
        if not any(s["id"] == sid for s in sessions):
            return False
        self.store.set_setting(ACTIVE_KEY, str(sid))
        return True

    def delete_session(self, sid: int) -> bool:
        """Delete a session and its transcript. Never leaves zero sessions; if the
        active one is removed, the most recent remaining session becomes active."""
        sessions = self._ensure_sessions()
        if not any(s["id"] == sid for s in sessions):
            return False
        was_active = self.active_session_id() == sid
        sessions = [s for s in sessions if s["id"] != sid]
        self.store.set_setting(self._hist_key(sid), "")
        if not sessions:
            now = time.time()
            sessions = [{"id": sid + 1, "title": "", "created": now, "updated": now}]
            self.store.set_setting(self._hist_key(sid + 1), "")
            self.store.set_setting(ACTIVE_KEY, str(sid + 1))
        elif was_active:
            newest = max(sessions, key=lambda s: s.get("updated", 0))
            self.store.set_setting(ACTIVE_KEY, str(newest["id"]))
        self._write_sessions(sessions)
        return True

    # --- transcript (active session) --------------------------------------

    def _load(self) -> list[Msg]:
        raw = self.store.get_setting(self._hist_key(self.active_session_id()))
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return [Msg(d["role"], d["content"]) for d in data]
        except (ValueError, KeyError, TypeError):
            return []

    def _save(self, msgs: list[Msg]) -> None:
        sid = self.active_session_id()
        self.store.set_setting(
            self._hist_key(sid),
            json.dumps([{"role": m.role, "content": m.content} for m in msgs]),
        )
        self._touch(sid)

    def history(self) -> list[Msg]:
        """The active session's transcript (oldest first), for the panel to render."""
        return self._load()

    def append(self, role: str, content: str) -> None:
        """Append a line to the transcript without a model turn (e.g. a hire result
        posted by the game once the CEO confirms)."""
        msgs = self._load()
        msgs.append(Msg(role, content))
        self._save(msgs)

    def persist_user(self, message: str) -> None:
        """Append the CEO's message now, so it's durable + echoes instantly — the
        worker then runs send(persist_user=False) and only appends the reply."""
        msgs = self._load()
        msgs.append(Msg("human", message))
        self._save(msgs)

    def clear(self) -> None:
        """Wipe the active session's transcript (keeps the session itself)."""
        self.store.set_setting(self._hist_key(self.active_session_id()), "")

    # --- prompt context ---------------------------------------------------

    def _roster_brief(self) -> str:
        """Who the terminal can actually delegate to right now."""
        agents = self.store.list_agents()
        if not agents:
            return ("CURRENT TEAM: nobody hired yet. You have no employees to "
                    "delegate to — if the CEO asks for real work, tell them they "
                    "need to hire that role first (Nokia phone -> Hire, or the "
                    "TalentWorks building in the city).")
        lines = []
        for a in agents:
            tag_ = f" ({a.dept})" if getattr(a, "dept", "") else ""
            lines.append(f"  - {a.name} - {a.role}{tag_}")
        return ("CURRENT TEAM (delegate by ROLE with delegate_to):\n"
                + "\n".join(lines))

    # --- URL reality check (anti-hallucination backstop) ------------------

    @staticmethod
    def _file_live(url: str) -> bool:
        """True if a file:// link points at something that actually exists on disk."""
        import urllib.parse
        path = urllib.parse.unquote(url[len("file://"):])
        return bool(path) and os.path.exists(path)

    @staticmethod
    def _url_live(url: str) -> bool:
        """True only if the URL actually responds (not DNS-dead, not 404). This is
        the hard guard: an invented vercel.app link won't resolve, so it's caught
        regardless of what the model claims."""
        if url.startswith("file://"):
            return CompanyTerminal._file_live(url)
        headers = {"User-Agent": "CompanyAI-terminal"}
        for method in ("HEAD", "GET"):
            try:
                req = urllib.request.Request(url, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=6) as r:
                    return getattr(r, "status", 200) < 500
            except urllib.error.HTTPError as e:
                return e.code not in (404, 410) and e.code < 500  # exists (maybe auth-walled)
            except Exception:
                continue   # HEAD unsupported / transient → try GET, then give up
        return False

    def _verify_urls(self, text: str) -> str:
        """Strip any URL in `text` that doesn't actually load, with a clear note —
        so the terminal physically cannot present a fake 'it's live at …' link."""
        seen, dead = set(), []
        for m in _URL_RE.finditer(text):
            url = m.group(0).rstrip(_URL_TRIM)
            if url in seen:
                continue
            seen.add(url)
            if not self._url_live(url):
                dead.append(url)
        for url in dead:
            text = text.replace(url, f"[dead link removed — {url} did not load]")
        if dead:
            text += ("\n\n[terminal] One or more links didn't check out (a web URL "
                     "that didn't load, or a file that isn't on disk), so I stripped "
                     "them — I won't hand you a dead link. For a real web URL an "
                     "Engineer must run the site in Daytona (serve_site); to open a "
                     "saved drive file locally, use local_link.")
        return text

    def _local_link_tool(self):
        """A tool that turns a company-drive path into a file:// link that opens the
        real file on the CEO's computer (the drive is mirrored to disk by fs_write)."""
        from langchain_core.tools import tool
        import urllib.parse

        @tool
        def local_link(path: str = "") -> str:
            """Return a clickable file:// link that opens a company-drive file (or
            folder) on the CEO's OWN computer — the drive is mirrored to real files on
            disk. Pass a drive path like '/landing/index.html' or '/apps/brief.md';
            for a built website folder, point at its index.html and it opens in the
            browser. Pass '' to open the whole drive folder in Finder. Use this when
            the CEO asks to open, see, or preview a file/site you saved to the drive."""
            from .company_fs import local_disk_path
            disk = local_disk_path(self.store, path)
            if not disk:
                return (f"No file on disk for {('/' + path.strip().lstrip('/')) or '/'}. "
                        "Save it to the drive first (drive_write), then link it.")
            url = "file://" + urllib.parse.quote(disk)
            return f"Open it on your computer: {url}"

        return local_link

    def _hire_tool(self):
        """A propose-a-hire tool. It does NOT hire: it parks a request the game
        surfaces for the CEO to confirm (and the game checks the budget there)."""
        from langchain_core.tools import tool

        @tool
        def hire_agent(role: str, reason: str = "") -> str:
            """PROPOSE hiring a new employee for a ROLE the company doesn't have yet
            (e.g. 'Engineer', 'Designer', 'Researcher', 'Blogger', 'Analyst'). This
            does NOT hire anyone by itself — it asks the CEO to confirm in the
            terminal, and the game checks the budget before any money is spent. Use
            it when a task needs a specialist nobody on the team holds. After calling
            it, STOP and tell the CEO to confirm; do not assume the role is filled."""
            self.pending_hire = {"role": (role or "").strip(),
                                 "reason": (reason or "").strip()}
            return (f"Hire request for a {(role or '').strip()} put to the CEO for "
                    "confirmation (the budget is checked before anyone is hired). "
                    "Tell the CEO to confirm it in the terminal; do NOT assume the "
                    "role is filled yet.")

        return hire_agent

    def _agent_by_role(self, role: str):
        """Resolve a role/name the CEO named to a real hired agent (or None)."""
        want = (role or "").strip().lower()
        if not want:
            return None
        agents = self.store.list_agents()
        for a in agents:                                  # exact role match first
            if a.role.lower() == want:
                return a
        for a in agents:                                  # then a loose role/name match
            if want in a.role.lower() or want in a.name.lower():
                return a
        return None

    def _ops_tools(self):
        """Scheduling tools: let the terminal set up & manage 24/7 autonomous jobs —
        a hired employee running an instruction on a schedule, unattended. The local
        worker (24/7 Operations) actually executes them; these just manage the rows."""
        from langchain_core.tools import tool
        from . import scheduling

        @tool
        def schedule_job(role: str, name: str, instruction: str,
                         schedule_type: str = "cron",
                         schedule_value: str = "0 9 * * *") -> str:
            """Set up a RECURRING / scheduled autonomous job: a hired employee runs
            `instruction` on a schedule, on their own, even when the CEO isn't looking,
            and saves results to the company drive. Use this for any 'every morning /
            hourly / daily / tomorrow at 9' style request.
            role: which hired employee runs it, by ROLE (e.g. 'Researcher', 'Engineer').
            name: a short label for the job.
            instruction: exactly what they should do each run.
            schedule_type + schedule_value, one of:
              - 'cron'     value like '0 9 * * 1-5'  (9am Mon–Fri) or '0 * * * *' (hourly)
              - 'interval' value = seconds between runs, e.g. '3600'
              - 'once'     value = local time 'YYYY-MM-DDTHH:MM'
            Only schedule a role that's actually hired."""
            agent = self._agent_by_role(role)
            if agent is None:
                return (f"Can't schedule — no '{role}' is on the team. Hire that role "
                        "first (hire_agent), or pick someone who is hired.")
            st = (schedule_type or "cron").strip().lower()
            if st not in ("cron", "interval", "once"):
                return f"schedule_type must be cron, interval, or once (got '{schedule_type}')."
            try:
                due = scheduling.initial_due(st, str(schedule_value).strip(),
                                             config.DEFAULT_TIMEZONE)
            except Exception as exc:
                return f"Couldn't parse the schedule '{schedule_value}': {exc}"
            job = self.store.create_job(
                agent.id, (name or "").strip() or "Scheduled job",
                (instruction or "").strip(), st, str(schedule_value).strip(),
                config.DEFAULT_TIMEZONE, scheduling.iso_utc(due))
            return (f"Scheduled '{job.name}' for {agent.name} ({agent.role}): "
                    f"{st} {schedule_value}, first run at {scheduling.iso_utc(due)} UTC. "
                    "It now runs automatically — track it in the 24/7 OPS tab.")

        @tool
        def list_scheduled_jobs() -> str:
            """List the company's scheduled/recurring autonomous jobs + their status."""
            jobs = self.store.list_jobs()
            if not jobs:
                return "No scheduled jobs yet."
            lines = []
            for j in jobs:
                who = self.store.get(j.agent_id)
                nm = who.name if who else j.agent_id
                state = "ON " if j.enabled else "OFF"
                lines.append(f"[{state}] {j.name} — {nm} — {j.schedule_type} "
                             f"{j.schedule_value} — next {j.next_run_at} (id {j.id})")
            return "\n".join(lines)

        @tool
        def cancel_job(job_id: str) -> str:
            """Pause (turn OFF) a scheduled job by its id from list_scheduled_jobs."""
            if self.store.get_job(job_id) is None:
                return f"No job with id {job_id}."
            self.store.set_job_enabled(job_id, False)
            return f"Paused job {job_id} — it won't run again until re-enabled."

        @tool
        def run_job_now(job_id: str) -> str:
            """Queue a scheduled job to run right now, on top of its normal schedule."""
            j = self.store.get_job(job_id)
            if j is None:
                return f"No job with id {job_id}."
            self.store.enqueue_manual_run(
                j.agent_id, j.instruction, scheduling.iso_utc(scheduling.utc_now()))
            return f"Queued '{j.name}' to run now — watch the OPS tab for the result."

        return [schedule_job, list_scheduled_jobs, cancel_job, run_job_now]

    def _firehose_tools(self) -> list:
        """Fire-and-forget dispatch — ALWAYS available, so the terminal never has to
        block the CEO on long work. Tasks go onto the team firehose and the in-process
        worker (started by the game) chews through them concurrently; results come back
        on their own. Works in-memory when Redis is off, and as a durable, crash-safe
        Redis Stream when it's on — same tool either way."""
        from langchain_core.tools import tool
        from . import task_queue

        @tool
        def dispatch_work(task: str, role: str = "") -> str:
            """Hand ONE piece of work to the team and return IMMEDIATELY — do NOT wait
            for it to finish. A hired employee (matching `role` when given, else any idle
            teammate) picks it up and runs it with their full toolset in the background;
            the result comes back on its own (it lands in the terminal log and the
            agent's chat). This is your DEFAULT for anything that takes real work — call
            it once per piece to kick off several at once. Only use delegate_to instead
            when you MUST quote the teammate's result inside THIS same reply."""
            t = (task or "").strip()
            if not t:
                return "Give the task a description to dispatch."
            if not self.store.list_agents():
                return ("Nobody's hired yet, so there's no one to pick this up — hire "
                        "someone first (hire_agent), then dispatch.")
            tid = task_queue.enqueue(t, role=(role or "").strip(), source="terminal")
            who = (role or "").strip() or "the team"
            return (f"Dispatched to {who} (task {tid}) — running in the background now; "
                    f"{task_queue.pending()} in flight. I won't wait on it.")

        @tool
        def fire_tasks(tasks: list[str], role: str = "") -> str:
            """Queue a BATCH of independent jobs at once (fire-and-forget) — e.g. 'draft
            5 launch tweets', 'research these 10 competitors'. Same as dispatch_work but
            many pieces in one call. `role` optionally targets a role; blank lets any
            idle teammate take them. Use whenever you do NOT need each result inline."""
            items = [t.strip() for t in (tasks or []) if t and t.strip()]
            if not items:
                return "Pass a list of concrete jobs to queue."
            if not self.store.list_agents():
                return "Nobody's hired yet — hire someone first, then fire tasks."
            for t in items:
                task_queue.enqueue(t, role=(role or "").strip(), source="terminal")
            tgt = f" for the {role.strip()}" if (role or "").strip() else ""
            return (f"Fired {len(items)} task(s) onto the firehose{tgt}; "
                    f"{task_queue.pending()} now pending. The team works them in the "
                    "background — I won't wait.")

        return [dispatch_work, fire_tasks]

    def _monitor_tools(self) -> list:
        """W&B Weave monitoring, straight on the terminal: read the live workforce
        leaderboard, per-agent economics, LLM spend, quality and recent failures.
        Both loaders gate on WANDB_API_KEY internally (return [] when Weave is off)."""
        try:
            from .weave_tools import load_weave_tools
            from .people_tools import load_people_tools
            return load_weave_tools(TERMINAL_ID, "CEO") + load_people_tools(TERMINAL_ID, "CEO")
        except Exception:
            return []

    # --- one turn ---------------------------------------------------------

    def _redis_tools(self) -> list:
        """Capabilities that light up only when Redis (+ Gemini embeddings) are
        connected: recall the company's long-term vector memory, and ask where
        people/shops are in the city (geo). Each is added only when its backend is
        live, so the prompt stays clean when Redis is off. (The task firehose is no
        longer gated here — see _firehose_tools, which works with or without Redis.)"""
        from langchain_core.tools import tool

        try:
            from . import agent_memory, city_geo
        except Exception:
            return []

        out: list = []

        if agent_memory.is_configured():
            @tool
            def recall_memory(query: str) -> str:
                """Search the company's long-term memory — past decisions, incidents,
                finished tasks and takeaways every agent has recorded — for anything
                semantically relevant to `query` (not keyword match). Check it before
                making a call so the team doesn't repeat itself or contradict an earlier
                decision."""
                hits = agent_memory.recall(query, k=5)
                if not hits:
                    return "No relevant memory found yet."
                return "\n".join(
                    f"- ({h['kind']}, by {h['agent']}; {h['score']}) {h['text']}"
                    for h in hits)
            out.append(recall_memory)

        if city_geo.is_configured():
            @tool
            def who_is_near(place: str = "", role: str = "") -> str:
                """Where people and shops are in the city RIGHT NOW (live Redis geo).
                `place` is what to look around: blank or 'me'/'ceo' = around the CEO, or a
                building/teammate name ('cafe', 'HQ'). `role` filters to a role (e.g.
                'Engineer' to find the nearest engineer). Nearest first, with distances."""
                spot = (place or "").strip().lower()
                if spot in ("", "me", "ceo", "i", "here"):
                    rows = city_geo.near_entity("ceo", radius=120, role=role.strip())
                    where = "you"
                else:
                    eid = city_geo.resolve(spot)
                    if not eid:
                        return f"Nothing called '{place}' is on the city map right now."
                    rows = city_geo.near_entity(eid, radius=120, role=role.strip())
                    where = place
                if not rows:
                    qual = f" ({role.strip()})" if role.strip() else ""
                    return f"Nobody{qual} near {where} right now."
                return f"Near {where}:\n" + "\n".join(
                    f"- {r['name']} ({r['role'] or r['kind']})"
                    + (f", in {r['room']}" if r.get('room') else "")
                    + f" — {r['dist']}m" for r in rows)
            out.append(who_is_near)

            @tool
            def who_is_in_room(room: str, role: str = "") -> str:
                """Who is in a given OFFICE ROOM right now (live Redis geo): pass a room
                name like 'Engineering', 'Sales', 'CEO Office', or a wing. `role`
                optionally narrows to one role. Use it to see who's where before pulling
                someone into a task or a meeting."""
                members = city_geo.in_room(room, role=role.strip())
                if not members:
                    return f"Nobody{(' (' + role.strip() + ')') if role.strip() else ''} in '{room}' right now."
                return f"In {room}:\n" + "\n".join(
                    f"- {m['name']} ({m['role'] or m['kind']})" for m in members)
            out.append(who_is_in_room)

            @tool
            def team_map() -> str:
                """A live snapshot of the whole company by location: every room and who's
                in it right now (from Redis geo). Use it to get the lay of the land before
                assigning work or calling a meeting."""
                groups = city_geo.room_summary()
                groups = [g for g in groups if g["room"] != "City"]  # shops aren't staff
                if not groups:
                    return "The city map is empty right now (is the game running?)."
                return "\n".join(
                    f"{g['room']}: " + ", ".join(
                        f"{m['name']} ({m['role'] or m['kind']})" for m in g["members"])
                    for g in groups)
            out.append(team_map)

        return out

    def _remember_turn(self, message: str, reply: str) -> None:
        """Record the directive + outcome into the company's shared vector memory, so
        future terminal turns (and every agent) can recall what the CEO asked and what
        happened. Best-effort + gated — never blocks or breaks a reply."""
        try:
            from . import agent_memory
            if not agent_memory.is_configured() or len(reply) < 40:
                return
            if reply.startswith("[") or "interrupted before finishing" in reply:
                return
            agent_memory.remember(
                f"CEO directive: {message}\nTerminal outcome: {reply[:600]}",
                agent_name="CEO Terminal", role="CEO", kind="directive")
        except Exception as exc:
            log.warning("terminal memory write skipped: %s", exc)

    def send(self, message: str, persist_user: bool = True,
             on_step=None, on_token=None, mentions=None) -> str:
        """Run one terminal turn; stream tokens/steps; return the final reply.

        `mentions`, if given, is a list of employees the CEO @-tagged in the panel
        ({id,name,role}). They're spliced into the prompt as HARD targets so the
        orchestrator routes the relevant work to exactly those teammates instead of
        guessing a role."""
        if persist_user:
            self.persist_user(message)
        history = self._load()[-HISTORY_WINDOW:]

        system = _PERSONA + "\n\n" + self._roster_brief()
        company_ctx = company.context_for(self.store)   # the CEO's company decisions
        if company_ctx:
            system += "\n\n" + company_ctx
        if mentions:
            who = "; ".join(f"{m['name']} (the {m['role']})" for m in mentions if m.get("role"))
            if who:
                system += (
                    "\n\nTAGGED TEAMMATES — the CEO @-mentioned " + who + ". Route the "
                    "relevant part of this directive DIRECTLY to them (delegate_to / "
                    "dispatch_work by their role), addressing them by name. Do not "
                    "reassign their part to anyone else.")
        # Recall relevant company memory for this directive and splice it in, so the
        # terminal acts on what's already been decided/done (no-op if memory is off).
        try:
            from . import agent_memory
            mem = agent_memory.recall_block(message, k=4)
            if mem:
                system += "\n\n" + mem
        except Exception:
            pass

        msgs = [("system", system)]
        msgs += [(m.role, m.content) for m in history]
        if not history or history[-1].content != message:
            msgs.append(("human", message))   # safety net if it wasn't persisted

        # The terminal's toolset: the shared drive, the comms bus, delegation (hand
        # real subtasks to real employees), and a hire PROPOSAL tool (the CEO
        # confirms + the game checks the budget — see hire_agent below).
        tools = (load_fs_tools(author_id=TERMINAL_ID, author_name="CEO")
                 + load_bus_tools(TERMINAL_ID, "CEO", "CEO")
                 + load_delegation_tools(TERMINAL_ID, "CEO", "CEO")
                 + self._firehose_tools()
                 + self._ops_tools()
                 + self._monitor_tools()
                 + self._redis_tools()
                 + [self._hire_tool(), self._local_link_tool()])

        collected: list[str] = []

        def _emit(tok):
            if tok is None:
                collected.clear()
            else:
                collected.append(tok)
            if on_token:
                on_token(tok)

        self.last_call = None
        with tag(agent_id=TERMINAL_ID, agent_name="Global AI Terminal",
                 role="CEO", kind="terminal"):
            try:
                reply, call = call_op(chat_attempt, self.llm, msgs, tools,
                                      on_step, _emit)
                self.last_call = call
            except Exception:
                partial = "".join(collected).strip()
                if not partial:
                    raise
                reply = f"{partial}\n\n_[interrupted before finishing]_"

        # HARD anti-hallucination guard: physically verify every URL loads before
        # the CEO sees it; dead/invented links are stripped with a note.
        reply = self._verify_urls(reply)

        # Re-load before appending: the CEO's message was persisted synchronously
        # by persist_user() on the main thread, so we don't want to clobber it.
        out = self._load()
        out.append(Msg("ai", reply))
        self._save(out)
        self._remember_turn(message, reply)   # learn from this turn (gated, best-effort)
        return reply

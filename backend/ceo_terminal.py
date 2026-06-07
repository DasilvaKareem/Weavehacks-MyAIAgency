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

It reuses the exact streaming + tracing path as a 1:1 chat (chat_attempt via
call_op), so the terminal panel streams tokens and live tool steps ("using
delegate_to") the same way the in-office chat does.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections import namedtuple

from . import company
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
# Settings key holding the terminal transcript (a JSON list of {role, content}).
HISTORY_KEY = "terminal_history"
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
    "pieces and hand each piece to the right employee with the `delegate_to` tool "
    "(addressed by ROLE, e.g. 'Engineer', 'Designer', 'Researcher', 'Analyst', "
    "'Blogger'). The teammate actually does the work with their own tools and "
    "returns a result — fold those results into a single, clear answer for the CEO.\n"
    "Delegate ONLY the specific piece each specialist should own; do simple things "
    "(quick facts, summaries, decisions, looking something up in the company drive) "
    "yourself. If the company hasn't hired someone for a needed role, say so plainly "
    "and suggest hiring them — don't pretend the work happened.\n"
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

    # --- transcript (settings-backed) -------------------------------------

    def _load(self) -> list[Msg]:
        raw = self.store.get_setting(HISTORY_KEY)
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return [Msg(d["role"], d["content"]) for d in data]
        except (ValueError, KeyError, TypeError):
            return []

    def _save(self, msgs: list[Msg]) -> None:
        self.store.set_setting(
            HISTORY_KEY,
            json.dumps([{"role": m.role, "content": m.content} for m in msgs]),
        )

    def history(self) -> list[Msg]:
        """The full terminal transcript (oldest first), for the panel to render."""
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
        self.store.set_setting(HISTORY_KEY, "")

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

    # --- one turn ---------------------------------------------------------

    def send(self, message: str, persist_user: bool = True,
             on_step=None, on_token=None) -> str:
        """Run one terminal turn; stream tokens/steps; return the final reply."""
        if persist_user:
            self.persist_user(message)
        history = self._load()[-HISTORY_WINDOW:]

        system = _PERSONA + "\n\n" + self._roster_brief()
        company_ctx = company.context_for(self.store)   # the CEO's company decisions
        if company_ctx:
            system += "\n\n" + company_ctx

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
        return reply

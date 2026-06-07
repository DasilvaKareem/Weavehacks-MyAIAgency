"""Bridge between the raylib game and the backend (SQL store + agent chat).

Deliberately has NO raylib dependency, so it can be unit-tested headlessly.

Two jobs:
  * persistence — hiring writes through to the SQLite store; the roster is
    restored on startup so agents survive a restart.
  * chat — `send()` runs the (blocking) Gemini call on a worker thread and
    returns immediately; the game polls `poll_reply()` each frame. The render
    loop therefore never blocks on the model.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import queue
import threading

log = logging.getLogger("company.link")
if not log.handlers:                 # the app configures no logging — give ours a voice
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[company.link] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

from backend.store import AgentRow, AgentStore, Message
from backend.chat import AgentChat
from backend.ceo_terminal import CompanyTerminal, TERMINAL_ID
from backend.ceo_terminal import HISTORY_KEY as TERMINAL_HISTORY_KEY
from backend.planner import plan_policy
from backend import composio_tools

# Key under which the player's CEO profile (appearance + name) is stored.
CEO_PROFILE_KEY = "ceo_profile"
# Completed quest-line task keys (the game's plot progress), stored as a JSON list.
TASKS_KEY = "task_progress"
# The idle-market state (asset prices, holdings, savings, last-seen), a JSON blob.
MARKET_KEY = "market_state"
FARM_KEY = "farm_state"
# Outfit ids the player has purchased (premium models + suit styles), a JSON list.
# Once an id is here the outfit is reusable for free on any CEO/agent.
UNLOCKS_KEY = "unlocked_outfits"
# The company's decided identity (name/pitch/customer/business model/pricing/brand/
# competitors). Canonical home of the CEO's decisions; the backend reads it from here
# (see backend/company.py) to brief every agent. JSON dict.
COMPANY_KEY = "company_profile"
# The player's cash on hand, persisted so the bank balance survives a restart.
CASH_KEY = "cash"
# Ids of the office lots the player has leased, so your offices survive a restart.
LEASES_KEY = "leased_lots"
# The in-game calendar's running day count, so the date survives a restart.
CALENDAR_KEY = "calendar_day"
# The GLB basename of the car the player owns (bought at the Auto Mall), or empty
# if they don't own one — so your ride survives a restart.
OWNED_CAR_KEY = "owned_car"


class CompanyLink:
    def __init__(self, db_path: str | None = None) -> None:
        self.store = AgentStore(db_path) if db_path else AgentStore()
        # Small pool: a couple of agents can be mid-reply at once without
        # letting the company flood the API. Mirrors the backend's scale stance.
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="agent-chat"
        )
        # A separate single worker for movement-policy planning, so an occasional
        # planning call never starves the chat pool (and vice-versa).
        self._plan_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="agent-plan"
        )
        # Worker for Composio status/connect calls (network), keyed by a string.
        self._composio_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="composio"
        )
        # Dedicated worker for the Weave quality leaderboard fetch, so a busy
        # composio/agent pool can never starve the MONITOR tab (a saturated shared
        # pool was leaving it stuck on "connecting" / empty during demos).
        self._lb_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="weave-lb"
        )
        # Worker for the MONITOR "have the Observability Engineer fix this agent"
        # action: a one-shot delegation (LLM + tools), kept off the leaderboard pool.
        self._fix_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="weave-fix"
        )
        self._chats: dict[str, AgentChat] = {}
        self._pending: dict[str, concurrent.futures.Future] = {}
        # Live tool-loop progress labels per agent, written from the chat worker
        # thread and drained by the panel each frame (thread-safe via Queue).
        self._steps: dict[str, queue.Queue] = {}
        # Streamed answer tokens per agent (str deltas; None = reset between
        # tool rounds), same producer/consumer pattern as _steps.
        self._tokens: dict[str, queue.Queue] = {}
        self._plans: dict[str, concurrent.futures.Future] = {}
        self._composio: dict[str, concurrent.futures.Future] = {}
        # Single worker for the LLM grant board (one application reviewed at a time).
        self._grant_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="grant-judge"
        )
        self._grant: concurrent.futures.Future | None = None
        # Single worker for the monthly customer-judge revenue call.
        self._customer_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="customer-judge"
        )
        self._customers: concurrent.futures.Future | None = None
        # Cached firehose backlog (background tasks in flight). Refreshed OFF the
        # render thread so the terminal never blocks the game loop on a Redis
        # round-trip — terminal_pending_tasks() returns this last-known value.
        self._pend_count = 0
        self._pend_at = 0.0
        self._pend_busy = False
        # The terminal is a NON-BLOCKING command line: directives are queued and a
        # background pump runs them one at a time, so the CEO can fire message after
        # message without ever waiting. _term_gen bumps when a turn finishes so the
        # panel knows to re-read the log; _term_busy drives the 'thinking' indicator.
        self._term_q: "queue.Queue[str]" = queue.Queue()
        self._term_pump: threading.Thread | None = None
        self._term_lock = threading.Lock()
        self._term_busy = False
        self._term_gen = 0
        # Turn Weave tracing ON for the WHOLE session up-front (best-effort, off the
        # render thread). Without this, weave.op/attributes are no-ops and nothing the
        # CEO does this session is traced — the MONITOR tab would only ever show stale
        # data and the agent you're talking to now wouldn't appear. Warming the client
        # here also means the leaderboard fetch is instant (no cold init) when MONITOR
        # is first opened, and pre-fetches one round so the tab is never empty on open.
        self._weave_ready = False
        threading.Thread(target=self._warm_weave, name="weave-warm", daemon=True).start()

    def _warm_weave(self) -> None:
        try:
            from backend.observability import init_weave

            if init_weave() is not None:
                self._weave_ready = True
                log.info("Weave tracing warmed at startup; pre-fetching leaderboard")
                self.refresh_leaderboard()   # pre-fetch so MONITOR has data on open
            else:
                log.warning("Weave not configured at startup (no WANDB_API_KEY) — "
                            "MONITOR will stay empty")
        except Exception as exc:
            log.warning("Weave warm-up failed: %r", exc)

    # --- persistence -------------------------------------------------------

    def hire(self, name: str, role: str, dept: str = "",
             llm_model: str | None = None, char_model: str | None = None,
             char_appearance: str | None = None) -> str:
        """Persist a hire; return its backend id (store on Character.backend_id).

        `char_appearance` is a JSON blob of the look chosen at hire time (skin/hair/
        hairstyle/eyes/suit indices) so a customized hire survives a restart."""
        return self.store.hire(name=name, role=role, dept=dept, model=llm_model,
                               char_model=char_model,
                               char_appearance=char_appearance).id

    def roster(self) -> list[AgentRow]:
        return self.store.list_agents()

    # --- CEO profile (first-launch onboarding) ----------------------------

    def load_ceo(self) -> dict | None:
        """The saved CEO profile (name + appearance), or None on first launch."""
        raw = self.store.get_setting(CEO_PROFILE_KEY)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def save_ceo(self, profile: dict) -> None:
        """Persist the CEO profile chosen in the onboarding tutorial."""
        self.store.set_setting(CEO_PROFILE_KEY, json.dumps(profile))

    def load_tasks(self) -> set[str]:
        """The set of completed quest-line task keys (empty on a fresh save)."""
        raw = self.store.get_setting(TASKS_KEY)
        if not raw:
            return set()
        try:
            return set(json.loads(raw))
        except ValueError:
            return set()

    def save_tasks(self, done: set[str]) -> None:
        """Persist completed task keys (the game's plot progress)."""
        self.store.set_setting(TASKS_KEY, json.dumps(sorted(done)))

    def load_market(self) -> dict | None:
        """The saved idle-market state (prices/holdings/savings), or None."""
        raw = self.store.get_setting(MARKET_KEY)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def save_market(self, state: dict) -> None:
        """Persist the idle-market state so it grows while you're away."""
        self.store.set_setting(MARKET_KEY, json.dumps(state))

    def load_farm(self) -> dict | None:
        """The saved idle-farm state (crop counts + accrued pot), or None."""
        raw = self.store.get_setting(FARM_KEY)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def save_farm(self, state: dict) -> None:
        """Persist the idle-farm state so it harvests while you're away."""
        self.store.set_setting(FARM_KEY, json.dumps(state))

    def load_cash(self) -> int | None:
        """The player's saved cash, or None on a fresh save (use STARTING_CASH)."""
        raw = self.store.get_setting(CASH_KEY)
        if raw is None or raw == "":
            return None
        try:
            return int(float(raw))
        except ValueError:
            return None

    def save_cash(self, amount: int) -> None:
        """Persist cash on hand so the balance survives a restart."""
        self.store.set_setting(CASH_KEY, str(int(amount)))

    def load_leases(self) -> set[str]:
        """Ids of leased office lots (empty on a fresh save)."""
        raw = self.store.get_setting(LEASES_KEY)
        if not raw:
            return set()
        try:
            return set(json.loads(raw))
        except ValueError:
            return set()

    def save_leases(self, ids: set[str]) -> None:
        """Persist which office lots you've leased, so your offices survive a restart."""
        self.store.set_setting(LEASES_KEY, json.dumps(sorted(ids)))

    def load_calendar(self) -> int:
        """The saved in-game day count (0 on a fresh save)."""
        raw = self.store.get_setting(CALENDAR_KEY)
        if not raw:
            return 0
        try:
            return int(float(raw))
        except ValueError:
            return 0

    def save_calendar(self, day: int) -> None:
        """Persist the in-game day count so the date survives a restart."""
        self.store.set_setting(CALENDAR_KEY, str(int(day)))

    def load_owned_car(self) -> str | None:
        """The model basename of the car the player owns, or None if they don't."""
        raw = self.store.get_setting(OWNED_CAR_KEY)
        return raw or None

    def save_owned_car(self, model: str | None) -> None:
        """Persist the owned car (empty string clears it, e.g. after selling)."""
        self.store.set_setting(OWNED_CAR_KEY, model or "")

    def load_flag(self, key: str) -> bool:
        """Read a one-off boolean flag (e.g. 'a one-time gift was claimed')."""
        return self.store.get_setting("flag_" + key) == "1"

    def set_flag(self, key: str, value: bool = True) -> None:
        """Persist a one-off boolean flag."""
        self.store.set_setting("flag_" + key, "1" if value else "")

    def reset_company(self) -> None:
        """Wipe the save for a New World: fire every agent and clear the CEO
        profile + task progress. Unlocked outfits are kept (they're paid for)."""
        for a in self.store.list_agents():
            self.store.fire(a.id)
        self.store.set_setting(CEO_PROFILE_KEY, "")
        self.store.set_setting(TASKS_KEY, "")
        self.store.set_setting(COMPANY_KEY, "")
        self.store.set_setting(MARKET_KEY, "")
        self.store.set_setting(CASH_KEY, "")
        self.store.set_setting(LEASES_KEY, "")
        self.store.set_setting(CALENDAR_KEY, "")
        self.store.set_setting(OWNED_CAR_KEY, "")
        self.store.set_setting(TERMINAL_HISTORY_KEY, "")    # wipe the AI terminal transcript too

    def load_unlocks(self) -> set[str]:
        """The set of purchased outfit ids (empty on a fresh save)."""
        raw = self.store.get_setting(UNLOCKS_KEY)
        if not raw:
            return set()
        try:
            return set(json.loads(raw))
        except ValueError:
            return set()

    def save_unlocks(self, ids: set[str]) -> None:
        """Persist purchased outfit ids (premium models + suit styles)."""
        self.store.set_setting(UNLOCKS_KEY, json.dumps(sorted(ids)))

    # --- company profile (the CEO's decisions every agent reads) -----------

    def load_company(self) -> dict:
        """The company's decided facts (empty dict on a fresh save)."""
        raw = self.store.get_setting(COMPANY_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def save_company(self, profile: dict) -> None:
        """Persist the company profile (read back by backend/company.py for agents)."""
        self.store.set_setting(COMPANY_KEY, json.dumps(profile))

    def history(self, agent_id: str) -> list[Message]:
        return self.store.history(agent_id)

    # --- chat (non-blocking) ----------------------------------------------

    def _chat(self, agent_id: str) -> AgentChat:
        chat = self._chats.get(agent_id)
        if chat is None:
            chat = AgentChat(agent_id, store=self.store)
            self._chats[agent_id] = chat
        return chat

    def is_busy(self, agent_id: str) -> bool:
        fut = self._pending.get(agent_id)
        return fut is not None and not fut.done()

    def react(self, agent_id: str, thumbs_up: bool = True) -> bool:
        """Attach the CEO's 👍/👎 to this agent's most recent traced reply.

        Lands as Weave feedback on the exact reply call, so the People Analytics
        Lead can rank agents by how the CEO actually rates them — not just by
        cost. No-op (False) when there's no traced reply yet or Weave is off.
        """
        chat = self._chats.get(agent_id)
        if chat is None:
            return False
        from backend.observability import react as _react

        return _react(getattr(chat, "last_call", None), "👍" if thumbs_up else "👎")

    def refresh_leaderboard(self) -> None:
        """Kick a background refresh of the live quality leaderboard (cached)."""
        fut = getattr(self, "_lb_pending", None)
        if fut is not None:
            if not fut.done():
                return                       # one already in flight
            self._drain_leaderboard(fut)     # capture its result before replacing it

        def _job():
            from backend.observability import init_weave
            from backend import weave_metrics as wm

            client = init_weave()
            if client is None:
                log.warning("leaderboard: Weave client is None (WANDB_API_KEY unset?)")
                return []
            rows = wm.workforce_leaderboard(wm.fetch_calls(client, 300))
            if len(rows) != getattr(self, "_lb_last_n", -1):
                log.info("leaderboard: fetched %d agent row(s) from Weave", len(rows))
                self._lb_last_n = len(rows)
            return rows

        self._lb_pending = self._lb_pool.submit(_job)

    def _drain_leaderboard(self, fut) -> None:
        """Read a finished leaderboard future into the cache (logs failures)."""
        try:
            self._lb_cache = fut.result() or []
        except Exception as exc:
            log.warning("leaderboard fetch failed: %r", exc)

    def poll_leaderboard(self) -> list:
        """Latest cached leaderboard rows (best-first); refreshes opportunistically."""
        fut = getattr(self, "_lb_pending", None)
        if fut is not None and fut.done():
            self._lb_pending = None
            self._drain_leaderboard(fut)
        return list(getattr(self, "_lb_cache", []))

    def diagnose_agent(self, agent_id: str) -> dict | None:
        """Why is this agent crashing, and how to fix it — for the MONITOR click-through.

        Kicks an off-thread Weave fetch the first time it's asked for an agent and
        returns its cached result ({failures: [...], fix: str}) once ready, or None
        while still loading. Cached per agent so reopening is instant.
        """
        cache = getattr(self, "_diag_cache", None)
        if cache is None:
            cache = self._diag_cache = {}
        pend = getattr(self, "_diag_pending", None)
        if pend is None:
            pend = self._diag_pending = {}

        fut = pend.get(agent_id)
        if fut is not None and fut.done():
            try:
                cache[agent_id] = fut.result()
            except Exception as exc:
                log.warning("diagnose_agent(%s) failed: %r", agent_id, exc)
                cache[agent_id] = {"failures": [], "fix": ""}
            pend.pop(agent_id, None)
        if agent_id in cache:
            return cache[agent_id]
        if fut is None:                     # nothing in flight — start one
            def _job(aid=agent_id):
                from backend.observability import init_weave
                from backend import weave_metrics as wm
                client = init_weave()
                if client is None:
                    return {"failures": [], "fix": ""}
                return wm.diagnose_agent(wm.fetch_calls(client, 400), aid)
            pend[agent_id] = self._lb_pool.submit(_job)
        return None                          # still loading

    def ask_observability_fix(self, agent_row: dict) -> None:
        """Hand a crashing agent to the Observability Engineer to diagnose + APPLY a
        fix (off-thread). The engineer uses its real Weave tools and apply_optimization,
        so this enacts a per-role override the workers obey next run — not just advice.
        No-op if a fix request is already in flight."""
        fut = getattr(self, "_fix_pending", None)
        if fut is not None and not fut.done():
            return
        aid = agent_row.get("agent_id", "")
        name = agent_row.get("name", "?")
        role = agent_row.get("role", "?")

        def _job():
            from backend.observability import init_weave
            from backend import weave_metrics as wm
            from backend.delegation import run_agent_once
            err = ""
            try:
                client = init_weave()
                if client is not None:
                    diag = wm.diagnose_agent(wm.fetch_calls(client, 400), aid)
                    fails = diag.get("failures") or []
                    err = fails[0]["error"] if fails else ""
            except Exception:
                pass
            task = (
                f"Our hired {role} '{name}' is showing a high crash rate"
                + (f" with this error: \"{err}\". " if err else ". ")
                + "Use your tools (agent_economics, optimization_verdict, recent_failures) "
                "to confirm the cause, then CALL apply_optimization to enact a concrete "
                "fix so their next run is healthier. Reply in ONE or two sentences with "
                "the exact change you made (or, if it was a transient error, say so)."
            )
            return run_agent_once("Observability Engineer", task, requester="CEO")

        self._fix_for = aid
        self._fix_pending = self._fix_pool.submit(_job)

    def observability_fix_pending(self) -> bool:
        fut = getattr(self, "_fix_pending", None)
        return fut is not None and not fut.done()

    def poll_observability_fix(self) -> tuple[str, str] | None:
        """(agent_id, engineer_reply) for the most recent fix request, or None.

        Drains the finished future into a cached result so the panel can keep
        showing the engineer's answer under that agent after it completes."""
        fut = getattr(self, "_fix_pending", None)
        if fut is not None and fut.done():
            self._fix_pending = None
            try:
                self._fix_result = (getattr(self, "_fix_for", ""), fut.result())
            except Exception as exc:
                self._fix_result = (getattr(self, "_fix_for", ""), f"[fix failed: {exc}]")
        return getattr(self, "_fix_result", None)

    def leaderboard_pending(self) -> bool:
        """True while a Weave leaderboard fetch is in flight (so the MONITOR tab can
        show 'connecting…' instead of a misleading 'no traces' on a cold open)."""
        fut = getattr(self, "_lb_pending", None)
        return fut is not None and not fut.done()

    def weave_enabled(self) -> bool:
        """True when Weave tracing is configured (WANDB_API_KEY set)."""
        try:
            from backend.observability import is_configured
            return is_configured()
        except Exception:
            return False

    def weave_dashboard_url(self) -> str:
        """Best URL to the live W&B Weave dashboard for this project, for the MONITOR
        tab's click-to-open. Derives entity/project from the live client, falling back
        to env vars, then to a project search."""
        import os
        project = os.getenv("WEAVE_PROJECT") or "company-ai"
        entity = os.getenv("WANDB_ENTITY") or os.getenv("WEAVE_ENTITY") or ""
        try:
            from backend.observability import init_weave
            client = init_weave()
            pid = getattr(client, "_project_id", "") or getattr(client, "project", "")
            if "/" in str(pid):                       # "entity/project"
                entity, project = str(pid).split("/", 1)
            else:
                entity = entity or getattr(client, "entity", "") or entity
        except Exception:
            pass
        if entity:
            return f"https://wandb.ai/{entity}/{project}/weave"
        return f"https://wandb.ai/search?q={project}"

    def send(self, agent_id: str, text: str) -> bool:
        """Schedule a message on a worker thread. False if one is still pending.

        The user's message is persisted synchronously HERE (before the worker
        runs), so it's durable the instant Send is pressed and the chat panel can
        echo it immediately — even if the model reply takes minutes. The worker
        is told not to persist it again.
        """
        if self.is_busy(agent_id):
            return False
        self.store.add_message(agent_id, "human", text)
        steps: queue.Queue = queue.Queue()
        tokens: queue.Queue = queue.Queue()
        self._steps[agent_id] = steps
        self._tokens[agent_id] = tokens
        self._pending[agent_id] = self._pool.submit(
            self._chat(agent_id).send, text, persist_user=False,
            on_step=steps.put,        # worker thread reports each step here
            on_token=tokens.put,      # ...and each streamed answer token here
        )
        return True

    def poll_reply(self, agent_id: str) -> str | None:
        """Return the agent's reply once ready (or an error string), else None."""
        fut = self._pending.get(agent_id)
        if fut is None or not fut.done():
            return None
        self._pending.pop(agent_id, None)
        self._steps.pop(agent_id, None)   # progress is done; drop the queues
        self._tokens.pop(agent_id, None)
        try:
            return fut.result()
        except Exception as exc:  # surface model/auth errors in the chat panel
            return f"[error: {exc}]"

    def poll_steps(self, agent_id: str) -> str | None:
        """Latest tool-loop progress label for this agent, or None if unchanged.

        Drains the queue and returns only the most recent label, so the panel
        always shows what the agent is doing *now*, never a stale backlog.
        """
        steps = self._steps.get(agent_id)
        if steps is None:
            return None
        latest = None
        while True:
            try:
                latest = steps.get_nowait()
            except queue.Empty:
                break
        return latest

    def poll_tokens(self, agent_id: str) -> list:
        """Streamed answer deltas since the last poll, in order.

        Each item is a text delta to append, or None meaning "reset" (a tool
        round's preamble was discarded). Order matters, so unlike poll_steps this
        returns the whole batch rather than just the latest.
        """
        tokens = self._tokens.get(agent_id)
        if tokens is None:
            return []
        out: list = []
        while True:
            try:
                out.append(tokens.get_nowait())
            except queue.Empty:
                break
        return out

    # --- global AI terminal (the CEO Desk) --------------------------------
    # The terminal isn't a roster agent, so it can't live in _chat()/the
    # messages table. It reuses the SAME non-blocking plumbing as 1:1 chat
    # (_pending/_steps/_tokens + poll_reply/poll_tokens/poll_steps), just keyed
    # by the fixed TERMINAL_ID, with its transcript kept in settings instead.

    def _terminal(self) -> CompanyTerminal:
        term = self._chats.get(TERMINAL_ID)
        if not isinstance(term, CompanyTerminal):
            term = CompanyTerminal(self.store)
            self._chats[TERMINAL_ID] = term
        return term

    # --- company drive (the terminal's Files browser) ---------------------

    def drive_files(self) -> list:
        """Every file on the shared company drive (FileRow rows, ordered by path),
        for the terminal's Files view."""
        return self.store.fs_list()

    def drive_local_path(self, path: str = "") -> str | None:
        """Real on-disk path for a drive virtual path (text mirror or asset
        disk_path), or None if it isn't on disk. Used to open a file natively."""
        from backend.company_fs import local_disk_path
        return local_disk_path(self.store, path)

    def drive_export(self, path: str, content: str | None) -> str | None:
        """Ensure a drive file is on disk so it can be opened natively, writing the
        text mirror if it's missing (old files predate the auto-mirror). Returns the
        real path, or None for a binary asset with no on-disk copy."""
        import os
        existing = self.drive_local_path(path)
        if existing or content is None:
            return existing
        dest = os.path.join(os.path.dirname(self.store.db_path), "drive",
                            path.strip().lstrip("/"))
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w") as fh:
                fh.write(content)
            return os.path.abspath(dest)
        except OSError:
            return None

    def terminal_history(self) -> list[Message]:
        """The active session's transcript (Message-like rows with .role/.content)."""
        return self._terminal().history()

    # --- terminal chat sessions -------------------------------------------

    def terminal_sessions(self) -> list:
        """All terminal conversations (newest-used first), each {id,title,active}."""
        return self._terminal().list_sessions()

    def terminal_new_session(self):
        """Start a fresh conversation and make it active; None if a turn is running."""
        if self.terminal_busy():
            return None
        return self._terminal().new_session()

    def terminal_switch_session(self, sid: int) -> bool:
        """Make a session active. False if a turn is running or the id is unknown."""
        if self.terminal_busy():
            return False
        return self._terminal().switch_session(sid)

    def terminal_delete_session(self, sid: int) -> bool:
        """Delete a session + its transcript. False if a turn is running."""
        if self.terminal_busy():
            return False
        return self._terminal().delete_session(sid)

    # --- 24/7 operations (terminal OPS tab) -------------------------------
    # Read + govern the always-on worker's data: scheduled jobs, run history, and
    # the approval queue. These touch worker rows, not the live terminal turn, so
    # they're safe to call even while a terminal message is streaming.

    def terminal_jobs(self) -> list:
        """All scheduled/recurring autonomous jobs (newest first)."""
        return self.store.list_jobs()

    def terminal_runs(self, limit: int = 40) -> list:
        """Recent autonomous run history (done/error/running)."""
        return self.store.list_runs(limit=limit)

    def terminal_approvals(self) -> list:
        """Risky tool calls a run paused on, waiting for the CEO's approve/reject."""
        return self.store.list_approvals()

    def terminal_agent_name(self, agent_id: str) -> str:
        a = self.store.get(agent_id)
        return a.name if a else agent_id

    def terminal_toggle_job(self, job_id: str, enabled: bool) -> None:
        self.store.set_job_enabled(job_id, enabled)

    def terminal_run_job_now(self, job_id: str) -> bool:
        """Queue a scheduled job to run immediately. False if the id is unknown."""
        j = self.store.get_job(job_id)
        if j is None:
            return False
        from backend.scheduling import iso_utc, utc_now
        self.store.enqueue_manual_run(j.agent_id, j.instruction, iso_utc(utc_now()))
        return True

    def terminal_decide_approval(self, approval_id: str, decision: str) -> None:
        """Resolve a pending approval: decision is 'approved' or 'rejected'."""
        self.store.decide_approval(approval_id, decision)

    def terminal_retry_run(self, run_id: str) -> None:
        self.store.retry_run(run_id)

    def terminal_pending_tasks(self) -> int:
        """How many fire-and-forget tasks are still working in the background.

        Returns the cached value IMMEDIATELY (never touches Redis on the caller's
        thread) and schedules an off-thread refresh at most every ~1.5s, so polling
        this every frame from the render loop costs nothing. 0 if the firehose is
        unavailable."""
        import threading
        import time as _t
        now = _t.monotonic()
        if now - self._pend_at > 1.5 and not self._pend_busy:
            self._pend_busy = True
            self._pend_at = now

            def _refresh() -> None:
                try:
                    from backend import task_queue
                    self._pend_count = int(task_queue.pending())
                except Exception:
                    self._pend_count = 0
                finally:
                    self._pend_busy = False

            threading.Thread(target=_refresh, name="pending-poll", daemon=True).start()
        return self._pend_count

    def terminal_append(self, role: str, content: str) -> None:
        """Append a line to the terminal transcript without a model turn (used by the
        game to post a hire result once the CEO confirms)."""
        self._terminal().append(role, content)

    def poll_terminal_hire(self):
        """A hire the terminal's hire_agent tool proposed, awaiting CEO confirm (or
        None). The game shows a Y/N prompt and checks the budget before hiring."""
        return self._terminal().pending_hire

    def clear_terminal_hire(self) -> None:
        """Drop the pending hire proposal (CEO confirmed or cancelled it)."""
        self._terminal().pending_hire = None

    def terminal_employees(self) -> list:
        """Hired employees as lightweight {id,name,role} dicts — for the terminal's
        @-mention picker (and any other roster autocomplete)."""
        return [{"id": a.id, "name": a.name, "role": a.role}
                for a in self.store.list_agents()]

    def terminal_send(self, text: str, mentions=None) -> bool:
        """Accept a directive WITHOUT blocking — the whole point of the terminal.

        The CEO's line is echoed + persisted immediately, then the turn is QUEUED and
        a background pump runs queued turns one at a time. The input is never locked:
        you can fire message after message and replies land in the log as they finish.
        `mentions` (employees the CEO @-tagged) ride along as hard delegation targets.
        Always returns True (an empty message is ignored)."""
        text = text.strip()
        if not text:
            return False
        self._terminal().persist_user(text)   # echo now, durable before the run
        self._term_q.put((text, list(mentions or [])))
        self._ensure_term_pump()
        return True

    def _ensure_term_pump(self) -> None:
        """Start the terminal pump thread if it isn't already draining the queue."""
        with self._term_lock:
            if self._term_pump is not None and self._term_pump.is_alive():
                return
            self._term_pump = threading.Thread(
                target=self._term_pump_loop, name="terminal-pump", daemon=True)
            self._term_pump.start()

    def _term_pump_loop(self) -> None:
        """Run queued directives sequentially (coherent context), streaming each
        turn's steps/tokens through the shared TERMINAL_ID queues the panel polls."""
        term = self._terminal()
        steps = self._steps.setdefault(TERMINAL_ID, queue.Queue())
        tokens = self._tokens.setdefault(TERMINAL_ID, queue.Queue())
        while True:
            # Atomically decide whether to take the next item or retire the pump, so
            # a concurrent terminal_send() either feeds us or starts a fresh pump.
            with self._term_lock:
                if self._term_q.empty():
                    self._term_pump = None
                    return
                text, mentions = self._term_q.get_nowait()
            self._term_busy = True
            try:
                term.send(text, persist_user=False,
                          on_step=steps.put, on_token=tokens.put, mentions=mentions)
            except Exception as exc:
                try:
                    term.append("ai", f"[error: {exc}]")
                except Exception:
                    pass
            finally:
                self._term_busy = False
                self._term_gen += 1        # a reply (or error) landed → panel refreshes

    def terminal_busy(self) -> bool:
        """True while a directive is running or still queued (drives the indicator)."""
        return self._term_busy or not self._term_q.empty()

    def terminal_queued(self) -> int:
        """How many directives are waiting behind the one currently running."""
        return self._term_q.qsize()

    def terminal_generation(self) -> int:
        """Counter that bumps each time a turn finishes; the panel re-reads the log
        when it changes (instead of polling a single in-flight future)."""
        return self._term_gen

    # --- movement-policy planning (non-blocking) --------------------------

    def is_planning(self, agent_id: str) -> bool:
        fut = self._plans.get(agent_id)
        return fut is not None and not fut.done()

    def request_policy(self, agent_id: str, role: str, name: str,
                       zone_names: list[str], context: str = "") -> bool:
        """Schedule LLM policy authoring on the planning worker. False if one is
        already in flight for this agent."""
        if self.is_planning(agent_id):
            return False
        self._plans[agent_id] = self._plan_pool.submit(
            plan_policy, agent_id, role, name, zone_names, context
        )
        return True

    def poll_policy(self, agent_id: str) -> dict | None:
        """Return the authored policy dict once ready, else None."""
        fut = self._plans.get(agent_id)
        if fut is None or not fut.done():
            return None
        self._plans.pop(agent_id, None)
        try:
            return fut.result()
        except Exception:
            return None

    # --- grant board (LLM-judged, non-blocking) ---------------------------

    def request_grant(self, application: str, company: dict) -> bool:
        """Submit a grant application to the LLM review board (off-thread). False if
        one is already under review."""
        if self._grant is not None and not self._grant.done():
            return False
        from backend.grants import judge_grant
        self._grant = self._grant_pool.submit(judge_grant, application, dict(company or {}))
        return True

    def poll_grant(self) -> dict | None:
        """Return the verdict dict once the board has decided, else None."""
        if self._grant is None or not self._grant.done():
            return None
        fut, self._grant = self._grant, None
        try:
            return fut.result()
        except Exception:
            return {"approved": False, "amount": 0, "program": "Small Business Grant",
                    "feedback": "The board couldn't process your application. Try again."}

    # --- customer judge (LLM → monthly revenue, non-blocking) -------------

    def request_customers(self, company: dict, team: int = 0) -> bool:
        """Kick off the monthly customer-panel revenue judgment (off-thread). False
        if one is already running."""
        if self._customers is not None and not self._customers.done():
            return False
        from backend.customer import judge_revenue
        self._customers = self._customer_pool.submit(judge_revenue, dict(company or {}), int(team))
        return True

    def poll_customers(self) -> dict | None:
        """Return {score, revenue, buzz} once the panel has judged, else None."""
        if self._customers is None or not self._customers.done():
            return None
        fut, self._customers = self._customers, None
        try:
            return fut.result()
        except Exception:
            return None

    # --- Composio connections (in-game OAuth) -----------------------------

    def request_composio_status(self, agent_id: str, toolkits) -> bool:
        """Kick off a connection-status check for an agent's toolkits (network)."""
        key = f"status:{agent_id}"
        if key in self._composio and not self._composio[key].done():
            return False
        self._composio[key] = self._composio_pool.submit(
            composio_tools.toolkit_status, list(toolkits))
        return True

    def poll_composio_status(self, agent_id: str) -> dict | None:
        """Return {toolkit: active|expired|missing} once ready, else None."""
        return self._poll(f"status:{agent_id}")

    def request_connect(self, toolkit: str) -> bool:
        """Kick off generating a browser auth URL for `toolkit` (network)."""
        key = f"connect:{toolkit}"
        if key in self._composio and not self._composio[key].done():
            return False
        self._composio[key] = self._composio_pool.submit(
            composio_tools.connect_url, toolkit)
        return True

    def poll_connect(self, toolkit: str) -> str | None:
        """Return the auth URL for `toolkit` once ready, else None."""
        return self._poll(f"connect:{toolkit}")

    def composio_refresh(self) -> None:
        """Drop cached tools so a freshly-authorized app is picked up next load."""
        composio_tools.clear_cache()

    def _poll(self, key: str):
        fut = self._composio.get(key)
        if fut is None or not fut.done():
            return None
        self._composio.pop(key, None)
        try:
            return fut.result()
        except Exception:
            return None

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._plan_pool.shutdown(wait=False, cancel_futures=True)
        self._composio_pool.shutdown(wait=False, cancel_futures=True)
        self._grant_pool.shutdown(wait=False, cancel_futures=True)
        self._customer_pool.shutdown(wait=False, cancel_futures=True)
        # Tear down any Daytona sandbox a Software Engineer agent spun up.
        try:
            from backend.daytona_tools import shutdown as daytona_shutdown
            daytona_shutdown()
        except Exception:
            pass

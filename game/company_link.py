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
import queue

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
        if fut is not None and not fut.done():
            return

        def _job():
            from backend.observability import init_weave
            from backend import weave_metrics as wm

            client = init_weave()
            if client is None:
                return []
            return wm.workforce_leaderboard(wm.fetch_calls(client, 300))

        self._lb_pending = self._composio_pool.submit(_job)

    def poll_leaderboard(self) -> list:
        """Latest cached leaderboard rows (best-first); refreshes opportunistically."""
        fut = getattr(self, "_lb_pending", None)
        if fut is not None and fut.done():
            self._lb_pending = None
            try:
                self._lb_cache = fut.result() or []
            except Exception:
                pass
        return list(getattr(self, "_lb_cache", []))

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
        """The terminal transcript (Message-like rows with .role/.content)."""
        return self._terminal().history()

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

    def terminal_send(self, text: str) -> bool:
        """Schedule a terminal turn on a worker thread. False if one's pending.

        The CEO's line is persisted synchronously here (so the panel can echo it
        immediately and it's durable before the possibly-minutes-long run), then
        the worker streams steps/tokens exactly like a 1:1 chat.
        """
        if self.is_busy(TERMINAL_ID):
            return False
        term = self._terminal()
        term.persist_user(text)
        steps: queue.Queue = queue.Queue()
        tokens: queue.Queue = queue.Queue()
        self._steps[TERMINAL_ID] = steps
        self._tokens[TERMINAL_ID] = tokens
        self._pending[TERMINAL_ID] = self._pool.submit(
            term.send, text, persist_user=False,
            on_step=steps.put, on_token=tokens.put,
        )
        return True

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
        # Tear down any Daytona sandbox a Software Engineer agent spun up.
        try:
            from backend.daytona_tools import shutdown as daytona_shutdown
            daytona_shutdown()
        except Exception:
            pass

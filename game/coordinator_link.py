"""Game-side bridge to the co-founder — the coordinator that runs the company.

The phone's "New Message" line texts your co-founder. Under the hood that's the
LangGraph company graph (backend/orchestrator.py): a message is a goal, the
co-founder (`ceo_plan`) breaks it into tasks, the agents (`worker`) do them, and
the co-founder (`ceo_review`) synthesises one report back — the "direct result".

Like CompanyLink/MeetingLink this has NO raylib dependency and never blocks the
render loop: `send()` schedules the run on the Orchestrator's own thread and
returns immediately; the panel drains `poll()` (delegation progress) and
`poll_reply()` (the final report) each frame.

The Orchestrator (and its langgraph/LLM deps) is created lazily on the first
message, so a missing key or import only degrades the phone's co-founder line
rather than breaking game start. `available()` reports whether it came up.
"""
from __future__ import annotations

# Default co-founder identity shown on the phone. Rename freely.
COFOUNDER_NAME = "Robin"


class CoordinatorLink:
    def __init__(self) -> None:
        self._orch = None          # backend Orchestrator, built on first send
        self._fut = None           # in-flight run future (the report)
        self._error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    def _ensure(self) -> bool:
        """Bring up the Orchestrator on demand. Returns False (and records the
        reason in `_error`) if the backend can't start."""
        if self._orch is not None:
            return True
        try:
            from backend import Orchestrator
            self._orch = Orchestrator()
            self._error = None
            return True
        except Exception as exc:                  # missing key, import, etc.
            self._error = str(exc)
            return False

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def busy(self) -> bool:
        return self._fut is not None and not self._fut.done()

    # --- send / poll (non-blocking) ---------------------------------------

    def send(self, text: str) -> bool:
        """Schedule a co-founder run for `text`. False if one is still pending
        or the backend failed to start (see `error`)."""
        text = text.strip()
        if not text or self.busy:
            return False
        if not self._ensure():
            return False
        try:
            self._fut = self._orch.submit(text)
            return True
        except Exception as exc:
            self._error = str(exc)
            return False

    def poll(self) -> list:
        """Drain delegation events since the last call: a list of AgentEvent
        (kind in plan|task_done|report|error). Empty when nothing's running."""
        if self._orch is None:
            return []
        return self._orch.poll_events()

    def poll_reply(self) -> str | None:
        """The co-founder's final report once the run finishes, else None."""
        if self._fut is None or not self._fut.done():
            return None
        fut, self._fut = self._fut, None
        try:
            return fut.result()
        except Exception as exc:
            return f"[error: {exc}]"

    def shutdown(self) -> None:
        if self._orch is not None:
            try:
                self._orch.shutdown()
            except Exception:
                pass
            self._orch = None

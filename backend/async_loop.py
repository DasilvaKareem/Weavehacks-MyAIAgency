"""One process-wide asyncio event loop, on a daemon thread.

Every synchronous caller that needs to run a backend coroutine — the chat tool
loop, MCP tool loading, the online reply scorer, the company graph — submits to
THIS loop instead of spinning up a throwaway ``asyncio.run()`` loop per call.

Why it matters: the shared, cached Gemini client (backend/llm.py) lazily binds
an aiohttp session to the event loop it first runs on. A fresh loop per call
leaves that session bound to a *closed* loop, so the next call raised
``RuntimeError: Timeout context manager should be used inside a task`` — which is
exactly what made hired agents show "100% crash" after their first reply. A
single long-lived loop keeps the session valid for the life of the process.
"""
from __future__ import annotations

import asyncio
import threading

_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def loop() -> asyncio.AbstractEventLoop:
    """The shared loop, started on first use (on its own daemon thread)."""
    global _loop
    with _lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever,
                             name="backend-aio", daemon=True).start()
        return _loop


def run(coro):
    """Run a coroutine to completion on the shared loop, blocking the caller.

    Must be called from a NON-loop thread (worker/chat threads, the game's pools);
    safe to call concurrently — the loop multiplexes the coroutines.
    """
    return asyncio.run_coroutine_threadsafe(coro, loop()).result()

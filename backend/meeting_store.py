"""Realtime store for agent-to-agent meetings, backed by Firebase RTDB.

Hybrid persistence: hired agents and 1:1 CEO chats stay in local SQLite
(`backend/store.py`) — instant and offline. Meetings need realtime fan-out to
several processes at once (the raylib game, the Pipecat voice service, any
dashboard), so they live in Firebase RTDB: every participant `listen()`s on the
shared transcript and gets each turn + presence change pushed instantly.

Everything is namespaced under FIREBASE_NS, so this game's data never collides
with the other apps sharing the Firebase project.

Config (env / .env):
    FIREBASE_SERVICE_ACCOUNT   path to the admin-SDK service-account JSON
    FIREBASE_DB_URL            the RTDB URL
    FIREBASE_NS                root namespace (default 'companyAI')

RTDB tree:
    /{ns}/conversations/{cid}            {kind, topic, members:{id:true}, status, createdAt}
    /{ns}/conversations/{cid}/messages/* {sender, content, ts}   <- the live channel
    /{ns}/presence/{cid}/{agentId}       {speaking, ts}
"""
from __future__ import annotations

import os
import queue
import threading
import uuid
from dataclasses import dataclass

_APP_NAME = "companyai"
_app = None
_lock = threading.Lock()

_SERVER_TS = {".sv": "timestamp"}


def _get_app():
    """Initialise (once) and return the named firebase_admin app."""
    global _app
    with _lock:
        if _app is not None:
            return _app
        import firebase_admin
        from firebase_admin import credentials

        sa = os.getenv("FIREBASE_SERVICE_ACCOUNT")
        url = os.getenv("FIREBASE_DB_URL")
        if not sa or not url:
            raise RuntimeError(
                "Firebase not configured. Set FIREBASE_SERVICE_ACCOUNT and "
                "FIREBASE_DB_URL in your environment / .env."
            )
        try:
            _app = firebase_admin.get_app(_APP_NAME)
        except ValueError:
            _app = firebase_admin.initialize_app(
                credentials.Certificate(sa), {"databaseURL": url}, name=_APP_NAME
            )
        return _app


def _ns() -> str:
    return os.getenv("FIREBASE_NS", "companyAI")


def available() -> bool:
    return bool(os.getenv("FIREBASE_SERVICE_ACCOUNT") and os.getenv("FIREBASE_DB_URL"))


@dataclass
class MeetingMessage:
    sender: str          # agent id, or 'ceo' / 'moderator'
    content: str
    ts: int = 0
    key: str = ""        # RTDB push key (ordering + dedupe)


class MeetingStore:
    """Read/write side of meetings. For realtime reads use `subscribe()`."""

    def __init__(self) -> None:
        self._app = _get_app()

    def _ref(self, path: str):
        from firebase_admin import db
        return db.reference(f"/{_ns()}/{path}", app=self._app)

    # --- meetings ----------------------------------------------------------

    def create_meeting(self, topic: str, members: list[str], cid: str | None = None) -> str:
        cid = cid or uuid.uuid4().hex[:12]
        self._ref(f"conversations/{cid}").set({
            "kind": "meeting",
            "topic": topic,
            "members": {m: True for m in members},
            "status": "open",
            "createdAt": _SERVER_TS,
        })
        return cid

    def post(self, cid: str, sender: str, content: str) -> str:
        ref = self._ref(f"conversations/{cid}/messages").push({
            "sender": sender, "content": content, "ts": _SERVER_TS,
        })
        return ref.key

    def messages(self, cid: str) -> list[MeetingMessage]:
        data = self._ref(f"conversations/{cid}/messages").get() or {}
        out = [MeetingMessage(sender=v.get("sender", ""), content=v.get("content", ""),
                              ts=v.get("ts", 0) or 0, key=k) for k, v in data.items()]
        out.sort(key=lambda m: m.ts)
        return out

    def set_presence(self, cid: str, agent_id: str, speaking: bool) -> None:
        self._ref(f"presence/{cid}/{agent_id}").set({"speaking": bool(speaking), "ts": _SERVER_TS})

    def close_meeting(self, cid: str) -> None:
        self._ref(f"conversations/{cid}/status").set("closed")

    def subscribe(self, cid: str) -> "MeetingSubscription":
        return MeetingSubscription(self, cid)


class MeetingSubscription:
    """Realtime listener for one meeting's messages.

    firebase-admin's `listen()` runs a background thread and invokes our callback
    per change; we push new messages into a thread-safe queue that the render
    loop drains with `poll()` each frame — the same non-blocking pattern as
    CompanyLink, so it never stalls the game.
    """

    def __init__(self, store: MeetingStore, cid: str) -> None:
        self.cid = cid
        self._q: "queue.Queue[MeetingMessage]" = queue.Queue()
        self._seen: set[str] = set()
        self._listener = store._ref(f"conversations/{cid}/messages").listen(self._on_event)

    def _on_event(self, event) -> None:
        data = event.data
        if data is None:
            return
        if event.path == "/" and isinstance(data, dict):
            # initial snapshot: {key: {sender,...}, ...}
            for k, v in data.items():
                self._emit(k, v)
        else:
            # a single child added/changed: path = '/<key>'
            self._emit(event.path.strip("/"), data)

    def _emit(self, key: str, v) -> None:
        if not isinstance(v, dict) or key in self._seen:
            return
        self._seen.add(key)
        self._q.put(MeetingMessage(sender=v.get("sender", ""), content=v.get("content", ""),
                                   ts=v.get("ts", 0) or 0, key=key))

    def poll(self) -> list[MeetingMessage]:
        """Drain newly-arrived messages without blocking. Call once per frame."""
        out: list[MeetingMessage] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        try:
            self._listener.close()
        except Exception:
            pass

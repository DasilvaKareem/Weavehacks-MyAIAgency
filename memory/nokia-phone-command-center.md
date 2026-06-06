---
name: nokia-phone-command-center
description: Drawable Nokia phone overlay — text the co-founder (coordinator) or any agent 1:1, or call them, without walking the office.
metadata:
  type: project
---

A command-center phone UI so the CEO can act without navigating the 3D world.
Open in the office with **N** (for Nokia) or the "Phone (N)" HUD button.

Pieces:
- `game/phone_panel.py` — `PhonePanel`, a code-drawn Nokia (navy body + green LCD,
  no sprite yet; swap art into `_draw_body`, screen logic all renders inside the
  LCD rect). Screen state machine: HOME → INBOX → MESSAGE / COFOUNDER thread /
  CONTACTS → AGENT thread or CALL. Reuses `chat_panel._wrap` and the
  `get_char_pressed` input pattern. Nav: arrows/Enter, mouse clicks on rows, or
  controller; long lists scroll via `_list_view` (keeps the cursor on-screen). In
  CONTACTS, Enter = Message, **C** = Call (agent greets in their own voice via
  `voice.speak`). On MESSAGE, **R** = Reply (opens the sender's agent thread).
- `game/inbox.py` — `Inbox` (post/messages newest-first/unread/mark_read),
  `InboxMessage`, and `InboxFeeder` (rate-limited, templated agent + NPC messages,
  no model call; raylib-free, caller passes the clock). The phone HOME shows the
  unread count and the HUD "Phone (N)" button shows "N new".
  Producers: real agent "finished work" messages are posted in main.py's
  `_reconcile_busy_agents` (landed background replies, previously discarded);
  ambient agent/NPC messages come from the feeder ticked each frame with
  `all_agents` + park `NpcBuilding` names. "NPCs" = park businesses, not characters.
- `game/coordinator_link.py` — `CoordinatorLink`, the co-founder bridge. The
  co-founder IS the company graph (`backend/orchestrator.py` Orchestrator): a
  message is a goal → `ceo_plan` delegates to `worker`s → `ceo_review` returns one
  report. Lazy-inits the Orchestrator on first send and degrades gracefully if the
  backend/key is missing (`error`). `COFOUNDER_NAME = "Robin"` lives here.
- Agent 1:1 messages reuse the existing `CompanyLink` (send/poll_reply/streaming),
  same backend as the walk-up [[storage-architecture]] chat.

main.py wiring: constructed at ~136-139; routed in the input chain (`elif
self.phone.open`), drawn in the office draw chain, opened via KEY_N + phone_btn,
shut down via `self.coordinator.shutdown()`. `_reconcile_busy_agents` skips
`phone.active_agent_id` so the phone keeps its own agent reply.

Note: KEY_T was already taken (daylight skip-phase debug) — that's why it's N.

Not done yet / follow-ups: co-founder always plans→delegates (no chit-chat
triage; could tweak `ceo_plan` prompt to answer trivial msgs directly + 0 tasks);
Call is a TTS-greeting stub, not two-way audio; phone is office-only (not park);
co-founder transcript is in-memory (not persisted). Sprite can replace `_draw_body`.

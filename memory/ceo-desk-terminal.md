---
name: ceo-desk-terminal
description: CEO Desk = a walk-up power desk that opens the full-screen "Global AI Terminal", a company-wide AI chat that delegates the CEO's orders to real employees.
metadata:
  type: project
---

The **CEO Desk** is an executive desk with a glowing terminal monitor placed in the
CEO's office room — `BuildingInterior.ceo_office()` picks the **top-floor east wing**
(falls back to any top wing → first wing → entry room, so every building has one,
including the 1-story Starter Office). Walk up + **E** opens the **Global AI Terminal**.

The terminal (`game/terminal_panel.py`, `TerminalPanel`) is a full-screen 90s
green-phosphor CRT ChatGPT-clone: streaming tokens, live tool-step line, scrollback,
push-to-talk (hold Ctrl) + TTS, scanlines. Backed by `backend/ceo_terminal.py`
(`CompanyTerminal`) — a chief-of-staff agent that knows the live roster + company
context and is handed `load_delegation_tools`/`load_bus_tools`/`load_fs_tools`, so
typing an order actually runs the right employees one-shot (via `delegation.delegate`)
and folds the results back. **It is NOT a roster agent** (no desk character): its id
`__company_terminal__` would violate the `messages.agent_id` FK to `agents`, so the
transcript lives as JSON in settings (`terminal_history`), wiped on New World.

**Honesty guardrails (added after it hallucinated a fake `vercel.app` URL):** both
`_PERSONA` and `delegation.run_agent_once` now forbid inventing URLs/links/paths/
teammate names — only tool-returned values may be reported; unconnected capabilities
(e.g. publish_site needs `COMPOSIO_API_KEY` + `COMPOSIO_USER_ID` + linked Vercel) must
be reported as blocked, never faked. Prompt rules alone did NOT stop Gemini from
hallucinating, so there's a **hard code guard**: `CompanyTerminal._verify_urls`
HTTP-checks every URL in a reply (`_url_live`, HEAD/GET, 6s) and strips any that
404/DNS-fail with a note — the terminal physically cannot present a fake "it's live
at …" link. Real working URLs come from **Daytona `serve_site`** (live preview link,
`DAYTONA_API_KEY` is set), NOT Vercel/`publish_site` (`COMPOSIO_USER_ID` absent +
key 401, so it can't deploy). Terminal links (http**and** `file://`) are clickable
and an "OPEN -> … [O]" prompt appears for any verified link (chat_panel
`_open_externally` → macOS `open`).

**Local files / "open it on my computer":** the company drive is mirrored to REAL
disk files by `store.fs_write` → `workspaces/<slug>/drive/<path>` (e.g. a built
`landing/index.html`). The terminal has a `local_link(path)` tool that returns a
`file://` link to that real file (folder → its index.html; `''` → drive root in
Finder), so the CEO can open saved files/sites locally — no server needed.
`_verify_urls` existence-checks `file://` links and strips dead ones just like web
URLs. Drive browsing also works via `load_fs_tools` (drive_list/read/search).

**In-terminal Files browser:** the terminal panel has a second screen — **Tab**
toggles chat ↔ **Files** (also clickable `[CHAT]`/`[FILES]` header tabs). It lists
every drive file (`CompanyLink.drive_files` → `store.fs_list`), with a preview pane:
text shows content, images render a cached thumbnail (`_file_texture`), webapp shows
the live URL. **Enter/O/click-selected** opens the file natively via
`_open_externally` → `CompanyLink.drive_export` (which materializes old DB-only text
files to the disk mirror first). Shared resolver: `backend.company_fs.local_disk_path`
(used by both the `local_link` tool and the panel). ↑↓ select, R refresh, Esc → chat.

**Hire from the terminal:** `hire_agent(role)` is a *proposal* tool — it only sets
`CompanyTerminal.pending_hire`; it can't spend money. The game polls it
(`CompanyLink.poll_terminal_hire`), the panel shows a `[Y]/[N]` confirm bar, and on Y
`Game._terminal_hire()` does the real budget/desk check + `_hire_candidate()` on the
main thread (`_match_role` maps 'engineer'→'Software Engineer' etc.), posting the
outcome back via `CompanyLink.terminal_append`.

Wiring: `CompanyLink.terminal_send()/terminal_history()` reuse the existing
`_pending/_steps/_tokens` + `poll_reply/poll_tokens/poll_steps` pipeline keyed by
`TERMINAL_ID`. Scene draws the desk only when `scene.show_ceo_desk` is set in
`_activate_room` (real office + room == `ceo_office()`). Follows the same walk-up+E
pattern as the records cabinet. Related: [[interactive-buildings]], [[floor-plans]],
[[building-interiors]], [[redis-agent-bus]], [[chat-tts]].

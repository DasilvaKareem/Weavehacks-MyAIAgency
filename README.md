# Company.AI

A 3D company-builder game. You're the **CEO**; you hire **AI agents** who do real
work. **raylib** (Python) is the frontend; **LangGraph + Gemini** will be the
multi-agent backend.

## Run

The game is now wired to the backend, so run it from the venv (which has both
raylib and the LangGraph/Gemini deps — see "Backend" below for setup):

```bash
.venv/bin/python main.py
```

Controls: **WASD** move · **Shift** sprint · **Space** jump · **right-drag / Q,E**
look · **wheel** zoom · **Hire Agent** button (or **□**) to hire · **click / Tab /
D-pad** select an agent · **F / △** talk to the selected (or nearest) agent · in
chat, **wheel** scrolls history and **Esc** leaves. Busy agents show a floating
"working…" badge. Press **J** in the office to manage 24/7 jobs. Hires persist to
a local SQL store and are restored next launch.

## Project layout

```
main.py            game loop + state (cash, hiring)
game/config.py     tunable constants (window, economy, layout, camera)
game/scene.py      3D world: floor, desks, orbital camera
game/entities.py   Character (CEO + agents); placeholder boxes or real models
game/ui.py         2D HUD overlay + buttons + floating name labels
game/assets.py     model loading + caching
assets/models/     <-- drop your .glb files here
```

## Using your 3D assets

raylib loads **.glb / .gltf / .obj / .iqm / .vox / .m3d** natively.
It does **not** load **.fbx** — convert those to `.glb` first, e.g.:

```bash
blender -b -P - <<'PY'
import bpy, sys
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.fbx(filepath="character.fbx")
bpy.ops.export_scene.gltf(filepath="character.glb", export_format="GLB")
PY
```

To use a model, drop it in `assets/models/` and set the filename on a Character:

```python
Character(name="Agent 1", role="Engineer", x=0, z=2, color=pr.SKYBLUE,
          model="office_worker.glb")
```

Missing files fall back to placeholder boxes, so the game always runs.

## Backend (next milestone) — Python version note

The current shell runs on **Python 3.9** (your system). But the latest
**LangGraph (≥1.0)** and **langchain-google-genai (≥2.0)** require **Python ≥3.10**.

**Recommendation:** install Python 3.11/3.12 and use a venv before wiring the
backend:

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-backend.txt
echo "GOOGLE_API_KEY=your_key_here" > .env
```

(On 3.9 you'd be pinned to older, unsupported backend versions.)

Then run the backend on its own (no game needed yet):

```bash
python -m backend "Launch a developer-tools startup"
```

### Backend architecture (`backend/`) — built to scale

The orchestration is a LangGraph **map-reduce** so the company scales with the
number of agents you hire, not the graph:

```
START → ceo_plan → (fan-out: one Send per task) → worker* → ceo_review → END
```

- `ceo_plan` (Gemini) decomposes the CEO's goal into per-role subtasks.
- `_dispatch` emits one `Send` per task → LangGraph runs the workers concurrently.
- `worker` is **stateless**, keyed by agent identity; a module-level semaphore
  (`MAX_CONCURRENT_AGENTS`) caps live model calls so hiring 100 agents queues
  rather than floods the API.
- `ceo_review` reduces every agent's result into an executive summary.
- `Orchestrator` runs the graph on a **daemon thread** with a thread-safe
  `submit()` / `poll_events()`, so the raylib loop never blocks on a model call.

Scale knobs (env vars): `COMPANY_AI_MAX_AGENTS`, `COMPANY_AI_MAX_TASKS`,
`COMPANY_AI_MODEL`, `COMPANY_AI_TIMEOUT` — see `backend/config.py`.

```
backend/graph.py         the map-reduce graph
backend/agents.py        CEO planner / worker / CEO reviewer node logic
backend/state.py         typed state + results reducer
backend/orchestrator.py  async runtime + thread bridge to the game loop
backend/llm.py           cached Gemini client
backend/config.py        scale + model knobs
backend/store.py         local SQLite store: hired agents + chat history
backend/chat.py          one-on-one conversation with a single agent
```

### Persistence + talking to an agent

Hired agents and their conversations are stored locally in **SQLite**
(`company.db`, via stdlib `sqlite3` — no extra dependency). Each agent is a row
in `agents`; every chat turn is a row in `messages`, so conversations survive a
restart and each agent has its own memory.

```bash
# hire an agent (persists to company.db)
python -m backend.chat hire "Ada" Engineer

# list your agents
python -m backend.chat

# talk to one — it remembers prior turns, even across sessions
python -m backend.chat <agent_id>
```

The schema is id-keyed with indexed foreign keys, so it ports to Postgres later
if the company outgrows a local file.

## Roadmap

- [x] M1 — 3D office shell: CEO, hire agents, camera, HUD
- [x] M2 — Backend: scalable LangGraph CEO + N workers (Gemini), map-reduce
- [x] M3 — UI ↔ backend: hires persist to SQL + restore; walk up and chat with an
  agent (F / △), replies on a worker thread, status shown in 3D
- [ ] M4 — Real corporate assets, animations, task board, economy

## 24/7 local worker

Company.AI can keep hired agents working while the 3D office is closed. Jobs,
heartbeat checklist entries, run history, trust tiers, and approvals live in the
same SQLite database as the roster.

```bash
# keep this process running in its own terminal
.venv/bin/python -m backend.worker_service

# create and inspect schedules headlessly
.venv/bin/python -m backend.jobs jobs list
.venv/bin/python -m backend.jobs jobs create-interval
.venv/bin/python -m backend.jobs heartbeat add
.venv/bin/python -m backend.jobs approvals list
```

Inside the office, open **Jobs (J)** to manage schedules, the company heartbeat
checklist, approvals, recent activity, and per-agent trust. New agents default to
`supervised`; critical actions such as outbound messages, deletes, host-shell
mutations, and firing always pause for CEO approval.

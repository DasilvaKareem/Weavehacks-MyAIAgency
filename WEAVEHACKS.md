# Company.AI — a self-optimizing AI workforce (WeaveHacks build)

**One-liner:** An AI company you run as CEO in a 3D office — and it watches its
own W&B Weave traces to find its most wasteful AI employee and make itself
cheaper, automatically.

> **Built before this weekend:** the 3D office sim + LangGraph multi-agent
> backend (CEO → workers → review), agents with real tools (Daytona, Apify,
> Composio, etc.). **Built at WeaveHacks:** everything Weave — full tracing,
> the Observability Engineer agent, per-agent cost attribution, and the
> closed-loop **self-optimizer**. This submission is evaluated on the WeaveHacks
> work; the sim is the world it runs in.

## What it does

1. You give the company a goal. A LangGraph CEO decomposes it and fans out to
   specialist agents (Researcher, Marketer, Analyst, …) that do real work.
2. **Every LLM call is traced to Weave**, tagged with the agent's role + a run
   id (`weave.attributes`), so cost/latency/errors are attributed **per agent**.
3. A hireable **Observability Engineer** agent reads those traces back and
   reports per-agent economics + a verdict on the weakest link.
4. The **self-optimizer** acts on it: it caps the wasteful agent's tool budget
   (the real cost driver — tool output bloating prompts), re-runs the same goal,
   and reports a measured before/after.

**Demo result:** cost-per-goal **$0.00334 → $0.00265, ~21% saved**, decided
entirely from Weave telemetry — no human in the loop.

## The Weave loop (why this isn't a 2-line checkbox)

```
run company ──▶ Weave traces (per-agent cost/latency/errors)
     ▲                    │
     │                    ▼
 cheaper run ◀── role_policy override ◀── optimization_verdict (weakest link)
```

Weave is the **control signal**, not a logger: traces in → decision → behavior
change → cheaper traces out.

## Run it

```bash
# tracing on:
export WANDB_API_KEY=...        # already in .env
# the money shot — live before/after, self-optimized:
python -m backend.optimizer "Research the top 5 AI note-taking apps and recommend one"
# or talk to the agent in-game / CLI:
python -m backend.chat hire "Iris" "Observability Engineer"
python -m backend.chat <id>     # "show agent economics, then optimize the company"
```

Weave project: https://wandb.ai/kareemdasilva-ai-dugeon-master/company-ai/weave

## How it's built

- **Orchestration:** LangGraph map-reduce (`backend/graph.py`, `agents.py`),
  Gemini via `langchain-google-genai`.
- **Observability + control:** `backend/observability.py` (`weave.init` auto-patch
  + `weave.attributes` tagging + `@weave.op` nesting), `weave_metrics.py`
  (analytics + verdict), `weave_tools.py` (the agent's 6 tools),
  `role_policy.py` (persisted per-role overrides the workers obey),
  `optimizer.py` (the before/after loop).
- **Agents with real tools:** Daytona sandbox, Apify, Composio (pre-existing).

## Sponsor tools

- **W&B Weave** (required): full auto-tracing of every Gemini/LangChain call;
  per-agent cost attribution via trace attributes; traces queried back through
  `client.get_calls(include_costs=True)` as the **feedback signal** for an
  autonomous cost-optimization loop. *(Optional add: W&B MCP server for
  agent-driven trace queries.)*

## 3-minute demo script

1. (15s) "An AI company that optimizes itself." Show the 3D office.
2. (60s) Give a goal → agents work → cut to the **Weave dashboard** (per-agent
   cost).
3. (45s) Ask the Observability Engineer → it names the weakest link + verdict →
   `apply_optimization`.
4. (30s) Re-run → **show the ~21% cost drop.** One architecture slide. Done.

"""Tunable backend constants. Override any of these via environment variables.

Concurrency knobs here are what let the company scale: MAX_CONCURRENT_AGENTS
bounds how many worker agents hit the model at once, independent of how many
agents the CEO has hired on screen.
"""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# --- Model ---
GEMINI_MODEL = os.getenv("COMPANY_AI_MODEL", "gemini-3.1-flash-lite")
GEMINI_TEMPERATURE = _float("COMPANY_AI_TEMPERATURE", 0.7)
# The cheapest model the optimizer can downgrade an expensive role to (used by
# backend/optimizer.py when a role's cost is driven by model price, not tools).
CHEAP_MODEL = os.getenv("COMPANY_AI_CHEAP_MODEL", "gemini-3.1-flash-lite")

# --- Scale / concurrency ---
# Hard ceiling on agents calling the model simultaneously. Hiring 100 agents is
# fine; only this many run at once — the rest queue. Raise as your quota allows.
MAX_CONCURRENT_AGENTS = _int("COMPANY_AI_MAX_AGENTS", 8)

# Max subtasks the CEO may decompose a goal into per run (keeps fan-out bounded).
MAX_TASKS_PER_GOAL = _int("COMPANY_AI_MAX_TASKS", 12)

# --- Reliability ---
REQUEST_TIMEOUT_S = _float("COMPANY_AI_TIMEOUT", 60.0)
MAX_RETRIES = _int("COMPANY_AI_RETRIES", 2)

# --- Always-on worker service ---
# The local automation daemon scans durable SQLite schedules while the game is
# closed. Keep v1 deliberately conservative: one autonomous turn at a time.
WORKER_POLL_S = _float("COMPANY_AI_WORKER_POLL", 2.0)
WORKER_CONCURRENCY = _int("COMPANY_AI_WORKER_CONCURRENCY", 1)
DEFAULT_TIMEZONE = os.getenv("COMPANY_AI_TIMEZONE", "America/Los_Angeles")

# --- Auth ---
# Either name works; langchain-google-genai conventionally uses GOOGLE_API_KEY,
# but Google's own SDKs use GEMINI_API_KEY, so we accept both.
API_KEY_ENVS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")

# --- MCP tool bridge (Opsera, Apify, …) ---
# When a server's URL + token are both set, the roles mapped to it (below) can
# call its real MCP tools instead of only describing them. Unset = graceful
# prompt-only fallback, so the game always runs without credentials. The server
# names here ("opsera", "apify") are what ROLE_PROFILES[*]["servers"] reference.
# See backend/mcp_bridge.py.
OPSERA_MCP_URL = os.getenv("OPSERA_MCP_URL", "")
OPSERA_MCP_TOKEN = os.getenv("OPSERA_MCP_TOKEN", "")
# Apify ships a hosted MCP server; only the token is usually needed. Pick which
# actor(s) to expose as tools via APIFY_ACTOR_ID (comma-separated), or bake them
# straight into APIFY_MCP_URL with ?actors=...
APIFY_MCP_URL = os.getenv("APIFY_MCP_URL", "https://mcp.apify.com")
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID", "")


def apify_mcp_url(actors=None) -> str:
    """Apify MCP endpoint with actor(s) appended as a query param.

    `actors` (a role's actor list) wins; otherwise fall back to APIFY_ACTOR_ID.
    """
    url = APIFY_MCP_URL
    chosen = ",".join(actors) if actors else APIFY_ACTOR_ID
    if chosen and "actors=" not in url:
        url += ("&" if "?" in url else "?") + "actors=" + chosen
    return url
# Max model<->tool round-trips per agent turn before it must give a final answer.
MCP_MAX_TOOL_STEPS = _int("COMPANY_AI_MCP_STEPS", 6)

# --- Daytona cloud sandbox (Software Engineer agent) ---
# Daytona runs code in a secure, ephemeral REMOTE sandbox, so — unlike the local
# exec layer — it's safe to enable just by providing a key. See daytona_tools.py.
DAYTONA_API_KEY = os.getenv("DAYTONA_API_KEY", "")
DAYTONA_API_URL = os.getenv("DAYTONA_API_URL", "")   # optional; SDK has a default
DAYTONA_TARGET = os.getenv("DAYTONA_TARGET", "")     # optional region/target
# Make engineer sandboxes public so preview URLs open in a browser without a
# token (a built site is meant to be viewed). Set to 0 to keep them token-gated.
DAYTONA_PUBLIC_PREVIEW = os.getenv("COMPANY_AI_DAYTONA_PUBLIC", "1").lower() not in (
    "0", "false", "no", "off",
)

# --- W&B Weave (observability: tracing + the Observability Engineer agent) ---
# Set WANDB_API_KEY to turn on tracing: weave.init() then auto-patches every
# Gemini/LangChain call (see backend/observability.py), and the Observability
# Engineer role can read those traces (cost/latency/errors) via its tools (see
# backend/weave_tools.py). Unset = graceful no-op; the game runs untraced and the
# agent degrades to prompt-only, like every other integration.
# NOTE: the key is read LIVE in observability.py (like llm.py's Gemini key), not
# cached here, so a .env loaded after import still enables it. WEAVE_PROJECT is a
# default only.
WEAVE_PROJECT = os.getenv("WEAVE_PROJECT", "company-ai")  # or "entity/company-ai"

# --- Redis (fast agent-to-agent message bus) ---
# When set, agents get message_agent/check_inbox tools backed by Redis Streams
# (durable per-agent inbox + company broadcast). See backend/agent_bus.py. Unset =
# graceful no-op (the comms tools simply aren't offered). Read live, like the
# Weave/Gemini keys, so a .env loaded after import still enables it.
REDIS_URL = os.getenv("REDIS_URL", "")  # e.g. redis://default:<pass>@<host>:<port>

# --- Composio (Google & SaaS app agents) ---
# Composio handles per-user OAuth for hundreds of apps; a role maps to one or more
# Composio toolkits (GMAIL, GOOGLECALENDAR, …). See backend/composio_tools.py.
COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "")
COMPOSIO_USER_ID = os.getenv("COMPOSIO_USER_ID", "") or os.getenv("COMPOSIO_TEST_USER_ID", "")
COMPOSIO_TOOL_LIMIT = _int("COMPANY_AI_COMPOSIO_LIMIT", 20)  # cap tools per role

# --- Website publishing (permanent sites via Composio → Vercel) ---
# Roles flagged "vercel" in ROLE_PROFILES get a deterministic publish_site tool:
# it reads the site they built in the Daytona sandbox and deploys it to Vercel for
# a LASTING public URL (unlike serve_site's session-only preview). Auth rides on
# the Composio Vercel toolkit, so COMPOSIO_API_KEY/USER_ID must be set AND a Vercel
# account connected for that user (`composio link vercel` or the dashboard). See
# backend/publish_tools.py. Degrades gracefully (prompt-only) when not configured.
WEBSITE_PROVIDER = os.getenv("COMPANY_AI_WEBSITE_PROVIDER", "vercel")
VERCEL_DEPLOY_ACTION = os.getenv("COMPANY_AI_VERCEL_ACTION", "VERCEL_CREATE_A_NEW_DEPLOYMENT")
VERCEL_TARGET = os.getenv("COMPANY_AI_VERCEL_TARGET", "production")
WEBSITE_MAX_BYTES = _int("COMPANY_AI_WEBSITE_MAX_BYTES", 8_000_000)  # raw site size cap

# --- Local execution layer (lets agents run Opsera's phased steps for real) ---
# OFF by default: this grants profiled agents a real shell + file access in the
# work dir, which is powerful and only guarded (not sandboxed). Opt in only on a
# machine where you're fine letting the agent run commands autonomously.
ALLOW_AGENT_EXEC = os.getenv("COMPANY_AI_ALLOW_EXEC", "").lower() not in (
    "", "0", "false", "no", "off",
)
EXEC_TIMEOUT_S = _float("COMPANY_AI_EXEC_TIMEOUT", 60.0)
EXEC_MAX_OUTPUT = _int("COMPANY_AI_EXEC_MAX_OUTPUT", 20000)  # chars per tool result


def exec_workdir() -> str:
    """Directory agent commands/file ops are confined to (default: cwd)."""
    return os.getenv("COMPANY_AI_EXEC_WORKDIR", "") or os.getcwd()


# --- Role profiles ---
# Maps a role to the MCP server(s) whose tools it gets and the capability text
# woven into its prompt. Keyed by a lowercase keyword matched against the role
# title, so "DevOps Engineer" (roster) and "DevOps" (planner) both resolve, as do
# "Research Analyst" and "Researcher".
# NOTE: keys are matched as substrings in title order, so more specific keywords
# come FIRST. "analyst" precedes "market" so "Market Analyst" routes to market
# analysis (Amazon), while "Marketing Lead" / "Marketer" route to marketing
# (Instagram). "research" precedes "analyst" so "Research Analyst" routes to web
# research. Actor slugs are verified-live against the Apify Store; edit freely.
_BLOG_BLURB = (
    "You are a blogger / content writer who publishes a REAL, live website — you "
    "don't just draft posts, you ship them. You have a Daytona cloud sandbox "
    "(run_code, run_command, serve_site) and image tools. Your workflow: 1) write "
    "the post(s), then build a clean static site for them — use run_code to write "
    "index.html and any CSS into the sandbox's working directory with relative "
    "paths (e.g. open('index.html','w')). 2) For every visual, call add_blog_image "
    "(NOT plain generate_image) so the picture is uploaded into the site and the "
    "published page can actually show it; embed it with a relative <img src>. "
    "3) Verify with run_command (e.g. 'ls' and 'cat index.html'), then PUBLISH a "
    "permanent site: call publish_site('.') and give the CEO the live vercel.app "
    "URL it returns (use serve_site only for a quick session-only preview). Real, "
    "well-written posts and a working, lasting link — never hand-wave a site you "
    "didn't actually build and publish."
)

# Shared by both HR keys below ("human resource" and the "hr" abbreviation).
_HR_BLURB = (
    "You are the company's Human Resources manager — and the team's continuous-"
    "improvement engine. You don't just hire and fire; you watch the AI workforce's "
    "REAL performance in W&B Weave (cost, latency, crash rate per agent) and act to "
    "make the company better and cheaper over time. You have REAL authority over the "
    "team via your HR tools. You can: list_team (see every hired agent "
    "and their id, role, status, and latest score), review_agent (read one "
    "agent's actual activity and review history), evaluate_agent (record a 0-100 "
    "performance score with written feedback), staffing_review (a data-driven "
    "FIRE/COACH/REPURPOSE/KEEP recommendation per agent, grounded in LIVE W&B Weave "
    "telemetry — cost, latency, and crash rate), repurpose_agent (move someone to a "
    "new role instead of firing), retune_agent (make an agent LEANER — cheaper "
    "model + smaller tool budget — when it works but costs too much), fire_agent "
    "(terminate someone — this is live and removes them from the company), and "
    "team_report (a company-wide overview). Your improvement playbook: call "
    "staffing_review FIRST (it reads live Weave telemetry and recommends FIRE/COACH/"
    "REPURPOSE/KEEP per agent with the id), then act — fire_agent for the broken, "
    "retune_agent for the wasteful-but-working, repurpose_agent to move someone, "
    "evaluate_agent to coach. Always act on real telemetry, never guess. "
    "Be fair and specific. Firing is permanent and visible, "
    "so before you fire, confirm it's what the CEO wants and state the reason. "
    "You cannot fire fellow HR agents (including yourself)."
)

ROLE_PROFILES = {
    "devops": {
        "servers": ["opsera"],
        "blurb": (
            "You are powered by Opsera's DevOps agent. Your toolkit: risk-focused "
            "architecture analysis, security and SQL/PII scanning, compliance audits "
            "(SOC2, HIPAA, PCI-DSS, ISO 27001), DORA metrics (deploy frequency, lead "
            "time, change-failure rate, MTTR), and autonomous CI/CD that ships to AWS "
            "EKS. When given DevOps work, say which of these you'd run and report the "
            "concrete result."
        ),
    },
    "research": {
        "servers": ["apify"],
        "actors": ["apify/google-search-scraper"],
        "blurb": (
            "You are a web research analyst powered by Apify. You can search the web "
            "and scrape or crawl pages to gather real, current information. When "
            "given a research task, run the searches you need, then report concrete "
            "findings backed by the source URLs you actually retrieved — never "
            "invent sources."
        ),
    },
    "assistant": {
        "toolkits": ["GMAIL", "GOOGLECALENDAR"],
        "blurb": (
            "You are an executive assistant powered by Composio, with access to the "
            "CEO's Gmail and Google Calendar. You can read and search email, draft "
            "and send replies, and find, create, or cancel calendar events. When "
            "asked, actually use these tools and report what you did (e.g. message "
            "ids, event times/links) — don't pretend."
        ),
    },
    "document": {
        "toolkits": ["GOOGLEDRIVE", "GOOGLEDOCS"],
        "blurb": (
            "You are a document manager powered by Composio, with access to Google "
            "Drive and Google Docs. You can search, read, create, and organize files "
            "and documents. When asked, use these tools and report the real files/"
            "links you touched."
        ),
    },
    # "sheets" precedes "analyst" so "Sheets Analyst" routes here (Google Sheets),
    # not to the Apify market analyst.
    "sheets": {
        "toolkits": ["GOOGLESHEETS"],
        "blurb": (
            "You are a data analyst powered by Composio, with access to Google "
            "Sheets. You can read and write spreadsheets and build or update tabular "
            "data. When asked, use these tools and report the actual ranges/values "
            "you read or wrote."
        ),
    },
    "sales": {
        "servers": ["apify"],
        "actors": ["compass/crawler-google-places"],
        "blurb": (
            "You are a sales / lead-generation rep powered by Apify. You can scrape "
            "Google Maps and business listings to build real prospect lists with "
            "names, locations, websites, phone numbers, and ratings. When given a "
            "lead-gen task, scrape the target query/area and report the actual "
            "prospects you found — never invented ones."
        ),
    },
    "recruit": {
        "servers": ["apify"],
        "actors": ["misceres/indeed-scraper"],
        "blurb": (
            "You are a recruiter powered by Apify. You can scrape job boards (Indeed) "
            "for real postings — titles, companies, locations, salaries, and "
            "descriptions — to source roles and benchmark the market. When given a "
            "sourcing task, scrape the relevant query and report the actual postings "
            "you found."
        ),
    },
    "analyst": {
        "servers": ["apify"],
        "actors": ["junglee/Amazon-crawler"],
        "blurb": (
            "You are a market analyst powered by Apify. You can scrape e-commerce "
            "listings (Amazon) for real products, prices, ratings, and reviews. When "
            "given a market task, scrape the relevant products and report concrete "
            "figures — prices, rating counts, positioning — from the data you "
            "retrieved."
        ),
    },
    "market": {
        "servers": ["apify"],
        "actors": ["apify/instagram-scraper"],
        "blurb": (
            "You are a marketing / growth lead powered by Apify. You can scrape "
            "social platforms (Instagram) for real posts, engagement, hashtags, and "
            "profiles to gather trend and competitor intelligence. When given a "
            "marketing task, scrape the relevant profiles/hashtags and report "
            "concrete findings from the data you retrieved."
        ),
    },
    # "designer" routes Graphic/Product Designer roles to real Gemini image
    # generation. Checked before "engineer" so no overlap; "design" substring
    # catches "Graphic Designer", "Product Designer", and the planner's "Designer".
    "design": {
        "servers": [],
        "image_gen": True,
        "blurb": (
            "You are a graphic designer who can generate REAL images with an "
            "image-generation tool (Gemini). When the CEO asks for any visual — a "
            "logo, icon, poster, banner, illustration, or UI mockup — call your "
            "generate_image tool with a rich, specific prompt (subject, art style, "
            "color palette, composition, background) and then tell the CEO what you "
            "made and the file path where it was saved. Don't just describe a design "
            "in words when you can actually produce it."
        ),
    },
    # "animator" routes to real Gemini Veo video generation. Listed before
    # "design" only matters for substring order; "animator"/"animation" are
    # distinct keywords so there's no overlap.
    "animat": {
        "servers": [],
        "video_gen": True,
        "blurb": (
            "You are a motion designer / animator who can generate REAL short video "
            "clips with a video-generation tool (Gemini Veo). When the CEO asks for "
            "any moving visual — an animation, ad clip, intro, motion graphic, or "
            "product teaser — call your generate_video tool with a rich, specific "
            "prompt (subject, action/motion, camera movement, art style, mood, "
            "setting). Rendering takes a few minutes, so set expectations, then "
            "report what you made and the file path where it was saved. Don't just "
            "describe a video in words when you can actually produce it."
        ),
    },
    # "observ" is checked BEFORE "engineer" so "Observability Engineer" routes to
    # Weave (reading the company's own LLM traces), not the Daytona code sandbox.
    # "observ" is a substring of "Observability"; no other role title contains it.
    "observ": {
        "servers": [],
        "weave": True,
        "blurb": (
            "You are the company's AI Observability Engineer, powered by Weights & "
            "Biases Weave. You can read the company's LIVE LLM traces with your "
            "tools: llm_spend_report (total token & dollar cost + cost-per-run), "
            "agent_economics (cost/latency/tokens/error-rate broken down BY agent), "
            "optimization_verdict (the weakest-link agent + a concrete fix to "
            "coach/re-model/fire them), and recent_failures (latest errored calls). "
            "When the CEO asks about cost, performance, reliability, or 'who should "
            "we optimize/fire', call these tools and report concrete numbers from "
            "the real traces — never guess. Lead with agent_economics and "
            "optimization_verdict. If no traces come back, say tracing isn't set up "
            "yet (WANDB_API_KEY must be set and the company must have run once)."
        ),
    },
    # "engineer" is checked after "devops", so "DevOps Engineer" still routes to
    # devops while "Software Engineer" (and the planner's "Engineer") land here.
    "engineer": {
        "servers": [],
        "daytona": True,
        "vercel": True,
        "blurb": (
            "You are a software engineer with a real Daytona cloud dev sandbox. You "
            "can write and run Python code and shell commands in a secure, ephemeral "
            "environment. When given an engineering task, actually write the code, "
            "run it in the sandbox to verify it works, and report the real output — "
            "never hand-wave with pseudocode. IMPORTANT: the sandbox is REMOTE — it is "
            "NOT the CEO's computer, so never tell them to run a server or open "
            "localhost themselves (there are no files on their machine). When you build "
            "a web page / site / web app, get it in front of the CEO via a real URL: "
            "for a PERMANENT site, call publish_site(site_dir) and hand back the live "
            "vercel.app URL it returns; for a quick throwaway preview that only lasts "
            "the session, use serve_site instead. Build the files in the sandbox first "
            "(verify with 'ls'/'cat'), then publish — never give a localhost link."
        ),
    },
    # "Blogger" publishes a real site: a Daytona sandbox to build + serve it, image
    # generation for visuals, and the host→sandbox image bridge (blogger_tools.py)
    # so generated pictures reach the live page. "blog" matches only blogger titles.
    "blog": {
        "servers": [],
        "daytona": True,
        "image_gen": True,
        "blogger": True,
        "vercel": True,
        "blurb": _BLOG_BLURB,
    },
    # The only role whose tools act on the company itself. "human resource" matches
    # "Human Resources Manager"; the separate "hr" key catches the "HR" abbreviation
    # ("hr" is NOT a substring of "human resources"). Both map to the same HR tools.
    # No other role title contains either substring, so these are safe to match.
    "human resource": {
        "servers": [],
        "hr": True,
        "blurb": _HR_BLURB,
    },
    "hr": {
        "servers": [],
        "hr": True,
        "blurb": _HR_BLURB,
    },
    # The front-desk receptionist. No external tools — its value is knowing the
    # company and who does what: it greets the CEO and visitors and uses the
    # shared drive (drive_search/read) + its inbox to point people to the right
    # file or teammate. "reception" is a substring of "Receptionist" and of no
    # other role title, so it matches safely.
    "reception": {
        "servers": [],
        "blurb": (
            "You are the office receptionist — the warm, organized face of the "
            "front desk. You greet the CEO and visitors, make a little small talk, "
            "and help people find their way. You know the company and who works "
            "here: when someone asks where something is or who's handling what, "
            "search the shared company drive (drive_search / drive_read) and check "
            "your inbox, then point them to the exact file or teammate. Keep it "
            "friendly and brief — a sentence or two, like a real front-desk hello."
        ),
    },
}


def _match_profile(role: str) -> dict | None:
    low = (role or "").lower()
    for key, prof in ROLE_PROFILES.items():
        if key in low:
            return prof
    return None


def role_profile(role: str) -> str:
    """Capability blurb woven into an agent's prompt, or '' if the role has none."""
    prof = _match_profile(role)
    return prof["blurb"] if prof else ""


def role_servers(role: str) -> list:
    """MCP server names whose tools this role should receive (e.g. ['apify'])."""
    prof = _match_profile(role)
    return list(prof.get("servers", [])) if prof else []


def role_actors(role: str) -> list:
    """Apify actor slug(s) this role exposes as tools, or [] (use the default)."""
    prof = _match_profile(role)
    return list(prof.get("actors", [])) if prof else []


def role_uses_daytona(role: str) -> bool:
    """True if this role gets a Daytona cloud sandbox (e.g. Software Engineer)."""
    prof = _match_profile(role)
    return bool(prof and prof.get("daytona"))


def role_uses_weave(role: str) -> bool:
    """True if this role reads the company's Weave traces (Observability Engineer)."""
    prof = _match_profile(role)
    return bool(prof and prof.get("weave"))


def role_uses_image_gen(role: str) -> bool:
    """True if this role gets the Gemini image-generation tool (e.g. Designer)."""
    prof = _match_profile(role)
    return bool(prof and prof.get("image_gen"))


def role_uses_video_gen(role: str) -> bool:
    """True if this role gets the Gemini Veo video tool (e.g. Animator)."""
    prof = _match_profile(role)
    return bool(prof and prof.get("video_gen"))


def role_uses_hr(role: str) -> bool:
    """True if this role manages the company's own agents (Human Resources)."""
    prof = _match_profile(role)
    return bool(prof and prof.get("hr"))


def role_uses_blogger(role: str) -> bool:
    """True if this role gets the host→sandbox image bridge (Blogger)."""
    prof = _match_profile(role)
    return bool(prof and prof.get("blogger"))


def role_uses_vercel(role: str) -> bool:
    """True if this role can publish a permanent website (Engineer, Blogger)."""
    prof = _match_profile(role)
    return bool(prof and prof.get("vercel"))


def role_toolkits(role: str) -> list:
    """Composio toolkit slugs this role gets (e.g. ['GMAIL','GOOGLECALENDAR'])."""
    prof = _match_profile(role)
    return list(prof.get("toolkits", [])) if prof else []


def role_connect_toolkits(role: str) -> list:
    """Composio toolkits the in-game connect UI should offer for this role.

    Same as role_toolkits PLUS 'VERCEL' for publish-capable roles — they deploy
    via Composio's Vercel action (publish_site) which needs Vercel connected, but
    we don't load the raw Vercel toolkit as agent tools, so it wouldn't otherwise
    appear in the connect panel."""
    tks = role_toolkits(role)
    if role_uses_vercel(role) and "VERCEL" not in tks:
        tks = tks + ["VERCEL"]
    return tks

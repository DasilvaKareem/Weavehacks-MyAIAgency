"""The company-building quest line — the game's plot as a checklist of tasks.

You start as a lowly "Idea Guy" who just got rejected and moved to a new city.
From there the whole game is ~40 ordered tasks across six chapters: name the
company, analyse competitors, win over co-founder Robin, launch a website, hire a
team, lease buildings, grow users, turn profitable, raise a Series A. Finishing
the last one beats the game.

Most tasks map onto systems the game already has, so they complete themselves as
you play: hiring a role, leasing a building, publishing a site, holding a meeting,
growing the team. The rest (the early narrative beats) are completed by the
prologue or by talking to your co-founder. `TaskBoard` tracks progress and
persists it; `auto` predicates over a small stats dict drive the automatic ones.

This module is pure data + bookkeeping — no rendering, no Game reference — so it
stays easy to test and to edit. The on-screen quest log lives in game/quest_log.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Task:
    key: str                       # stable id (used for save data + auto hooks)
    chapter: str                   # chapter/heading this task lives under
    title: str                     # short imperative ("Name your company")
    desc: str                      # one-line "how / why"
    # Optional predicate over the live stats dict (see TaskBoard.refresh). When it
    # returns True the task auto-completes. None => completed by story/manual.
    auto: Callable[[dict], bool] | None = None
    # If set, the player completes this from the to-do list itself: clicking it
    # opens a text field with this prompt and the typed answer is stored on the CEO
    # profile under `field`. Tasks with neither `auto` nor `ask` are click-to-do.
    ask: str | None = None         # prompt shown in the input box
    field: str | None = None       # profile key the answer is saved under
    reward: int = 0                # seed cash granted on completion

    @property
    def manual(self) -> bool:
        """True if the player completes this from the to-do list (no auto hook)."""
        return self.auto is None


def _has_role(role: str) -> Callable[[dict], bool]:
    return lambda s: role in s.get("roles", ())


def _leased(key: str) -> Callable[[dict], bool]:
    return lambda s: key in s.get("leased", ())


def _team(n: int) -> Callable[[dict], bool]:
    return lambda s: s.get("agents", 0) >= n


# Ordered quest line. Chapters are just headings; order is the list order.
TASKS: list[Task] = [
    # --- Chapter 1: The Idea -------------------------------------------------
    Task("city_tour",   "The Idea", "Get to know the city",     "Visit Fresh Market and chat with the city guide.",),
    Task("name",        "The Idea", "Name your company",        "Every empire needs a name.",
         ask="What's the company called?", field="company_name", reward=500),
    Task("pitch",       "The Idea", "Write your one-line pitch", "What do you do, in a sentence?",
         ask="Pitch it in one line", field="pitch", reward=500),
    Task("customer",    "The Idea", "Name your target customer", "Who is this actually for?",
         ask="Who's your customer?", field="customer", reward=500),
    Task("competitors", "The Idea", "Size up the competition",   "Note who you're up against.",
         ask="Who are your main competitors?", field="competitors", reward=750),
    # field is "business_model" (NOT "model" — that key is the CEO's avatar gltf in
    # the appearance profile; reusing it would swap the player's character model).
    Task("model",       "The Idea", "Sketch your business model", "How does this make money?",
         ask="How does it make money?", field="business_model", reward=750),
    Task("cofounder",   "The Idea", "Win over co-founder Robin",  "Buy Robin a coffee and pitch them.",
         ask="Why should Robin bet on you?", field="cofounder_pitch", reward=2000),
    Task("seed",        "The Idea", "Raise your seed money",       "Pitch an angel investor for a first check.",
         ask="Pitch your idea — why fund it?", field="seed_pitch", reward=5000),

    # --- The Business Model Canvas (workshop at the Startup Incubator) -------
    # The canvas's nine blocks; customer segments (customer) and revenue streams
    # (business_model/pricing) are covered above, so these are the other seven.
    Task("value_prop",     "Business Model Canvas", "Define your value proposition", "The core promise to customers.",
         ask="What's your core value proposition?", field="value_prop", reward=500),
    Task("channels",       "Business Model Canvas", "Map your channels",            "How you reach and deliver to customers.",
         ask="How do you reach customers?", field="channels", reward=400),
    Task("relationships",  "Business Model Canvas", "Plan customer relationships",  "How you win, keep, and grow customers.",
         ask="How do you keep customers?", field="relationships", reward=400),
    Task("key_resources",  "Business Model Canvas", "List your key resources",      "What you must have to deliver.",
         ask="What key resources do you need?", field="key_resources", reward=400),
    Task("key_activities", "Business Model Canvas", "Name your key activities",     "The most important things you do.",
         ask="What are your key activities?", field="key_activities", reward=400),
    Task("partnerships",   "Business Model Canvas", "Line up key partners",         "Who you rely on to make it work.",
         ask="Who are your key partners?", field="partnerships", reward=400),
    Task("cost_structure", "Business Model Canvas", "Map your cost structure",      "What it costs to run the business.",
         ask="What are your biggest costs?", field="cost_structure", reward=500),

    # --- Chapter 2: Set Up Shop ---------------------------------------------
    Task("office",      "Set Up Shop", "Lease your first office", "Walk up to a building and lease it.",
         auto=lambda s: bool(s.get("leased"))),
    Task("intern",      "Set Up Shop", "Take on your first intern", "An intern's waiting in the park — they'll work for free once you have an office."),
    Task("engineer",    "Set Up Shop", "Hire your first engineer", "Someone has to build it.",
         auto=_has_role("Engineer")),
    Task("logo",        "Set Up Shop", "Design a logo",            "Give a Designer the brief.",
         ask="Describe the logo you want", field="logo", reward=600),
    Task("domain",      "Set Up Shop", "Register a domain",        "Stake your claim on the web.",
         ask="What domain do you want?", field="domain", reward=600),
    Task("website",     "Set Up Shop", "Launch your website",      "Publish a real site (Engineer/Blogger).",
         auto=lambda s: s.get("website", False)),
    Task("brand",       "Set Up Shop", "Choose your brand colors", "Make it look like you.",
         ask="Describe your brand & colors", field="brand", reward=600),

    # --- Chapter 3: Build the Product ---------------------------------------
    Task("mvp",         "Build the Product", "Ship the MVP",          "The smallest thing worth shipping."),
    Task("analytics",   "Build the Product", "Integrate analytics",   "You can't grow what you can't measure.",
         auto=lambda s: s.get("analytics", False)),
    Task("feedback",    "Build the Product", "Set up a feedback form", "Hear what users actually think."),
    Task("researcher",  "Build the Product", "Hire a researcher",      "Turn questions into answers.",
         auto=_has_role("Researcher")),
    Task("research",    "Build the Product", "Run market research",    "Validate before you build more."),
    Task("iterate",     "Build the Product", "Ship your first update", "Listen, then improve."),

    # --- Chapter 4: Get Growth ----------------------------------------------
    Task("designer",    "Get Growth", "Hire a designer",         "Make it beautiful.",
         auto=_has_role("Designer")),
    Task("landing",     "Get Growth", "Polish the landing page", "First impressions convert."),
    Task("blog",        "Get Growth", "Publish your first blog", "Have the Blogger ship a post."),
    Task("social",      "Get Growth", "Set up social media",     "Be where the customers are."),
    Task("campaign",    "Get Growth", "Run a marketing campaign", "Put fuel on the fire."),
    Task("users100",    "Get Growth", "Reach 100 users",          "Your first real traction."),

    # --- Chapter 5: Scale Up -------------------------------------------------
    Task("marketer",    "Scale Up", "Hire a marketer",          "Growth needs an owner.",
         auto=_has_role("Marketer")),
    Task("lease_eng",   "Scale Up", "Lease the Engineering Wing", "Room for the build team.",
         auto=_leased("eng")),
    Task("team5",       "Scale Up", "Grow the team to 5",       "A real crew now.",
         auto=_team(5)),
    Task("analyst",     "Scale Up", "Hire an analyst",          "Make the numbers talk.",
         auto=_has_role("Analyst")),
    Task("dashboard",   "Scale Up", "Build a metrics dashboard", "One screen, the whole company."),
    Task("pricing",     "Scale Up", "Set your pricing",         "Decide what you're worth.",
         ask="How do you price it?", field="pricing", reward=500),
    Task("revenue",     "Scale Up", "Make your first revenue",  "The first dollar is the hardest."),

    # --- Chapter 6: Build a Company -----------------------------------------
    Task("job",         "Build a Company", "Set up a 24/7 job",      "Put the company on autopilot.",
         auto=lambda s: s.get("jobs", 0) >= 1),
    Task("meeting",     "Build a Company", "Hold an all-hands",      "Get everyone in one room.",
         auto=lambda s: s.get("meetings", 0) >= 1),
    Task("lease_research", "Build a Company", "Lease the Research Labs", "A home for discovery.",
         auto=_leased("research")),
    Task("lease_design", "Build a Company", "Lease the Design Studio", "Where the craft lives.",
         auto=_leased("design")),
    Task("team10",      "Build a Company", "Grow the team to 10",    "A company, not a project.",
         auto=_team(10)),
    Task("users1k",     "Build a Company", "Reach 1,000 users",      "Momentum you can feel."),
    Task("lease_finance", "Build a Company", "Lease the Finance Tower", "Mind the money.",
         auto=_leased("finance")),
    Task("profitable",  "Build a Company", "Turn profitable",        "More in than out."),
    Task("series_a",    "Build a Company", "Raise a Series A",       "The big leagues. You win."),
]

# chapter -> tasks, in order (for the quest log's grouped display)
CHAPTERS: list[str] = []
for _t in TASKS:
    if _t.chapter not in CHAPTERS:
        CHAPTERS.append(_t.chapter)

# key -> Task, so callers (e.g. the park's quest-stop buildings) can show a task's
# title/desc and validate that a building's `task` key really exists.
TASK_BY_KEY: dict[str, "Task"] = {_t.key: _t for _t in TASKS}


@dataclass
class TaskBoard:
    """Tracks which tasks are done and what to do next. `done` is a set of task
    keys; everything else is derived. Pure bookkeeping — persistence is done by
    the caller (CompanyLink.save_tasks) from `done`."""

    done: set[str] = field(default_factory=set)

    # -- queries ------------------------------------------------------------
    def is_done(self, key: str) -> bool:
        return key in self.done

    def current(self) -> Task | None:
        """The first not-yet-done task — the active objective."""
        for t in TASKS:
            if t.key not in self.done:
                return t
        return None

    def progress(self) -> tuple[int, int]:
        return (len(self.done), len(TASKS))

    def beaten(self) -> bool:
        return len(self.done) >= len(TASKS)

    # -- mutation -----------------------------------------------------------
    def complete(self, key: str) -> bool:
        """Mark a task done. Returns True if this newly completed it."""
        if key in self.done or key not in _KEYS:
            return False
        self.done.add(key)
        return True

    def refresh(self, stats: dict) -> list[Task]:
        """Auto-complete any task whose `auto` predicate now holds. Returns the
        tasks that *newly* completed this call (so the caller can toast them)."""
        newly: list[Task] = []
        for t in TASKS:
            if t.key not in self.done and t.auto is not None:
                try:
                    if t.auto(stats):
                        self.done.add(t.key)
                        newly.append(t)
                except Exception:
                    pass
        return newly


_KEYS = {t.key for t in TASKS}

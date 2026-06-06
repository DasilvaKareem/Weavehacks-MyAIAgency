"""Company.AI — 3D office front end, wired to the LangGraph/Gemini backend.

You (the CEO) walk the office and hire agent characters that stand at desks.
Hiring persists to the local SQL store; the roster is restored on launch. Select
an agent (D-pad, or Tab) and press F / △ to talk to it one-on-one — chat runs on
a worker thread so the render loop never blocks.

Run:  .venv/bin/python main.py   (needs backend deps + a key in .env)
"""
from __future__ import annotations

import faulthandler
import json
import math
import random

# Print a real Python->C stack to stderr if a native call (raylib/GL) segfaults,
# so the occasional hard crash leaves a trace instead of just dying silently.
faulthandler.enable()

import pyray as pr

from game import config, gamepad, roster, furniture, navgrid, locomotion, zones, commands, floorplan, interior, daylight, season, tasks, todo
from game import park as parkmod
from game.park import Park, load_lots as load_park
from game.shop import ShopPanel, load_catalog
from game.marketplace import MarketplacePanel, load_catalog as load_agents
from game.assets import ModelRegistry
from game.scene import Scene
from game.camera import ThirdPersonCamera
from game.player import Player
from game.ui import Button, HireDialog, draw_hud, draw_world_labels
from game.entities import Character, make_ceo
from game.onboarding import OnboardingScreen
from game.dossier_panel import DossierPanel
from game.investor_panel import InvestorPanel
from game.prologue import Prologue
from game.menu import MainMenu
from game.company_link import CompanyLink
from game.chat_panel import ChatPanel
from game.drive_panel import DrivePanel
from game.jobs_panel import JobsPanel
from game.meeting_link import MeetingLink
from game.meeting_panel import MeetingPanel
from game.coordinator_link import CoordinatorLink, COFOUNDER_NAME
from game.phone_panel import PhonePanel
from game.pedestrians import Pedestrians
from game.inbox import Inbox, InboxFeeder, short as _inbox_short
from game.behavior import BotBrain, BotContext, Director, default_policy, P_MEETING

# Distance (world units) within which the CEO can talk to an unselected agent.
TALK_RANGE = 2.6

# What the person at each quest-stop says before you fill out their form, so a
# stop reads like a conversation, not a popup. Keyed by the to-do's task key.
QUEST_LINES = {
    "customer":       "Welcome in. Let's get you on the books - who exactly are you building this for?",
    "competitors":    "Smart founders know the field. So tell me - who are you up against?",
    "business_model": "Let's talk money. How does this thing actually turn a profit?",
    "logo":           "We'll mock something up for you. What should your logo look like?",
    "domain":         "Let's stake your claim online. What domain do you want?",
    "brand":          "Time to look the part. Describe your brand for me - colors, vibe, all of it.",
    "pricing":        "Let's not leave money on the table. How are you going to price this?",
    "cofounder":      "(slides a coffee across the table)  Alright - pitch me. Why should I bet on you?",
    # The Business Model Canvas workshop (Startup Incubator) — one block per line.
    "value_prop":     "Let's start at the heart of the canvas. What's the core value you promise customers?",
    "channels":       "Now - how do you actually reach and deliver to those customers?",
    "relationships":  "How will you win, keep, and grow your customers over time?",
    "key_resources":  "What are the key resources you can't run this business without?",
    "key_activities": "What are the most important activities your company has to do well?",
    "partnerships":   "No one builds alone. Who are the key partners you'll lean on?",
    "cost_structure": "Last block: where does the money go? What are your biggest costs?",
}

# Agent role pool (label + accent color used on name tags / fallback boxes)
ROLES = [
    ("Engineer", pr.SKYBLUE),
    ("Researcher", pr.GREEN),
    ("Designer", pr.MAGENTA),
    ("Analyst", pr.ORANGE),
    ("Marketer", pr.VIOLET),
]

# Desk grid (in tiles): columns x rows of workstations behind the CEO. The 4th
# row (8) is headroom unlocked by leasing offices in the park.
DESK_COLS = [3, 5, 7, 9, 11, 13]
DESK_ROWS = [2, 4, 6, 8]

BASE_DESKS = 18          # capacity with just HQ (first 3 desk rows)
DESKS_PER_LEASE = 3      # extra capacity per office leased in the park


def _load_env() -> None:
    try:
        import os

        from dotenv import load_dotenv

        # Load the .env sitting next to main.py explicitly. Bare load_dotenv()
        # relies on find_dotenv() walking up from the caller frame, which misses
        # the file in some launch contexts; an explicit path is reliable.
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        load_dotenv(env_path)
    except ImportError:
        pass


def desk_slot(index: int) -> tuple[int, int]:
    """Grid cell (col, row) for the Nth agent's desk."""
    row = DESK_ROWS[(index // len(DESK_COLS)) % len(DESK_ROWS)]
    col = DESK_COLS[index % len(DESK_COLS)]
    return col, row


class Game:
    def __init__(self) -> None:
        _load_env()
        self.company_name = "Company.AI"
        self.cash = config.STARTING_CASH
        self.registry = ModelRegistry()
        self.daylight = daylight.DayCycle()   # day/night lighting for office + park
        self.season = season.SeasonClock()    # Summer->Autumn->Winter tree foliage
        self.plans = floorplan.load_plans()

        self.mode = "office"
        self.park = Park(load_park())
        self.pedestrians = Pedestrians()              # ambient sidewalk crowd (park)
        # The building whose interior is currently active (starts at HQ).
        self.current_building = next((b for b in self.park.buildings if b.status == "hq"),
                                     self.park.buildings[0] if self.park.buildings else None)

        # Each building is a graph of rooms (lobby/elevator-lobby/wings); `self.room`
        # is the room we're standing in and `self.plan` is that room's FloorPlan.
        self.interior = interior.for_building(self.current_building, self.plans)
        self.room = self.interior.rooms[self.interior.entry_room]   # start in the lobby
        self.plan = self.room.plan(self.plans)
        zones.set_active(self.plan)
        locomotion.set_bounds(*self.plan.bounds())
        self.scene = Scene(self.plan)
        self.scene.set_plan(self.plan, seed=self.room.seed)   # lobby's own decor seed

        ceo = make_ceo(self.plan.cols / 2 - 0.5, self.plan.rows - 2, config.CEO_MODEL)
        ent = self.plan.point("entrance") or self.plan.grid_to_world(
            self.plan.cols / 2 - 0.5, self.plan.rows - 2)
        ceo.x, ceo.z = ent
        # Master roster (every agent) + the subset standing in the active room.
        # bot_ctx/Director/meeting hold the *active* lists, so those are mutated in
        # place (clear+extend) on a room switch, never reassigned.
        self.all_agents: list[Character] = []
        self.all_brains: list[BotBrain] = []
        self.characters: list[Character] = [ceo]
        self.agents: list[Character] = []
        self.brains: list[BotBrain] = []
        self.player = Player(ceo)
        self.camera = ThirdPersonCamera((ceo.x, ceo.y, ceo.z))
        self.selected = -1  # index into self.agents; -1 = nothing selected
        self._office_spawn = (ceo.x, ceo.z)

        # Backend: SQL persistence + one-on-one chat, off the render thread.
        self.link = CompanyLink()
        # Purchased outfit ids (premium marketplace models + premium CEO suits). One
        # unlock makes that outfit reusable for free on any CEO/agent; persists.
        self.unlocked = self.link.load_unlocks()
        # The plot: a to-do list of company-building tasks, most auto-completing as
        # you play (hire a role, lease a building, grow the team). Progress persists.
        self.taskboard = tasks.TaskBoard(self.link.load_tasks())
        self.todo = todo.TodoList()
        self.dossier = DossierPanel()      # view/edit the company decisions agents read
        self.investor = InvestorPanel()    # pitch the VC for a funding round
        self.todo.quest_keys = {k for n in self.park.npc if n.is_quest_stop
                                for k in n.task_keys()}
        self.chat = ChatPanel(self.link)
        self.hire_dialog = HireDialog()
        self.shop = ShopPanel(load_catalog())
        self.market = MarketplacePanel(load_agents())
        self.meeting_link = MeetingLink(self.link.store)
        self.meeting = MeetingPanel(self.meeting_link, self.agents)
        self.drive = DrivePanel(self.link.store)   # company file system browser
        self.jobs = JobsPanel(self.link.store)     # schedules, approvals, activity
        self.coordinator = CoordinatorLink()       # co-founder = the company graph
        # Inbox: messages that come TO the CEO — agents' finished work + status,
        # and the park businesses (NPCs) reaching out. The feeder drips ambient
        # agent/NPC messages; landed background replies are posted in the loop.
        self.inbox = Inbox()
        self.inbox_feeder = InboxFeeder()
        # The Nokia command center: text the co-founder or any agent, read the
        # inbox. Contacts are the whole roster (any room).
        self.phone = PhonePanel(self.link, self.coordinator,
                                lambda: self.all_agents, self.inbox, self.taskboard)
        self.inbox.post("Company.AI",
                        "Welcome! Your team and the neighborhood reach you here. "
                        "Open the phone (N) and tap a message to read it.",
                        kind="system", subject="Welcome to your phone", ts=0.0)
        self._buy_seq = 0
        self.used_names: set[str] = set()

        self.bot_ctx = BotContext(nav=None, ceo=ceo, agents=self.agents)
        self.director = Director(self.brains)
        self.chat.command_handler = self.handle_chat_command   # NL movement commands in chat
        self._chatting: Character | None = None   # bot held still while the CEO talks to it
        self._planned: set[str] = set()           # agent ids whose policy is applied/requested
        self._meeting_active = False              # are bots currently gathered for a meeting?
        self._gathered: list[BotBrain] = []       # brains pulled into the current meeting
        self.elevator_open = False                # the floor-select menu is up
        self._e_cooldown = 0                       # frames to swallow E after a mode/room switch

        # Launch lands on the home screen (New World / Continue). "New World" wipes
        # the save and runs the story prologue; "Continue" resumes the saved company.
        # OnboardingScreen stays for re-editing the CEO later (O).
        self.menu = MainMenu()
        self.menu_active = True
        self.onboarding = OnboardingScreen()
        self.prologue = Prologue()
        self.ceo_profile = self.link.load_ceo()
        # The company's decided identity (name/pitch/customer/business model/…). This
        # is the brain the agents read (backend/company.py). Seed name+pitch from an
        # older save where they lived on the CEO profile.
        self.company = self.link.load_company()
        if self.ceo_profile:
            if not self.company.get("name") and self.ceo_profile.get("company_name"):
                self.company["name"] = self.ceo_profile["company_name"]
            if not self.company.get("pitch") and self.ceo_profile.get("pitch"):
                self.company["pitch"] = self.ceo_profile["pitch"]
        if self.company.get("name"):
            self.company_name = self.company["name"]
        self._quest_input = None       # the quest-stop NPC whose text field is open
        self._quest_task = None         # which of its to-do keys is being captured
        self._quest_buf = ""
        self.onboarding_active = False
        self._onboarding_to_park = False
        self.prologue_active = False
        if self.ceo_profile is not None:
            self._apply_ceo_profile(self.ceo_profile)

        self._restore_agents()
        self._show_room(self.room.key)            # populate the active room's agents
        self._rebuild_nav()
        # You begin out in the city with no office of your own — leasing your first
        # building is a to-do, not a given. (The office interior above is just the
        # lazily-built default for whatever building you eventually walk into.)
        self._enter_park()

    def _new_world(self) -> None:
        """Start a fresh company: wipe the save, clear the in-memory world, and
        kick off the prologue. Buildings reset to just-HQ via a fresh Park; rooms
        rebuild themselves when the player next walks into one."""
        self.link.reset_company()
        self.cash = config.STARTING_CASH
        self.company_name = "Company.AI"
        self.ceo_profile = None
        self.company = {}
        self._quest_input, self._quest_task, self._quest_buf = None, None, ""
        self.taskboard = tasks.TaskBoard(set())
        self.phone.board = self.taskboard
        for lst in (self.all_agents, self.all_brains, self.agents, self.brains):
            lst.clear()
        self.characters[:] = [self.player.ch]
        self.selected = -1
        self.park = Park(load_park())                 # fresh leases (only HQ)
        self.current_building = next((b for b in self.park.buildings if b.status == "hq"),
                                     self.park.buildings[0] if self.park.buildings else None)
        self.interior = interior.for_building(self.current_building, self.plans)
        self.prologue = Prologue()
        self.prologue_active = True

    # The to-do/quest field names map onto the canonical company-profile keys the
    # backend reads (backend/company.py _FIELDS). "company_name" -> "name".
    _COMPANY_FIELD = {"company_name": "name", "pitch": "pitch", "customer": "customer",
                      "business_model": "business_model", "competitors": "competitors",
                      "brand": "brand", "pricing": "pricing", "industry": "industry",
                      "domain": "domain", "logo": "logo"}

    def _set_company_field(self, field: str, value: str) -> None:
        """Record one company decision so every agent's brain reads it next prompt."""
        key = self._COMPANY_FIELD.get(field, field)
        self.company[key] = value
        self.link.save_company(self.company)
        if key == "name":
            self.company_name = value

    def _do_dossier_action(self, action) -> None:
        """Apply an edit from the Company Dossier: save the decision to the company
        profile (read by every agent's brain). Doesn't touch task progress."""
        if action and action[0] == "set":
            self._set_company_field(action[1], action[2])

    def _rounds_raised(self) -> set:
        """Funding rounds already closed (persisted on the company profile)."""
        return set(self.company.get("rounds_raised", []))

    def _do_investor_action(self, action) -> None:
        """Land a funding round: pay the lump sum, record it so it can't be raised
        twice, and shout it to the inbox. Series A is the win — it ticks that to-do."""
        if not action or action[0] != "raise":
            return
        rnd = action[1]
        raised = self._rounds_raised()
        if rnd.key in raised:
            return
        raised.add(rnd.key)
        self.company["rounds_raised"] = sorted(raised)
        self.link.save_company(self.company)
        self.cash += rnd.amount
        if rnd.key == "seriesa" and self.taskboard.complete("series_a"):
            self.link.save_tasks(self.taskboard.done)
        self.inbox.post("Apex Ventures",
                        f"Wire's on the way - {rnd.name} round closed for ${rnd.amount:,}. "
                        f"Don't make us regret it.",
                        kind="system", subject=f"✓ {rnd.name} raised", ts=pr.get_time())

    def _do_todo_action(self, action) -> None:
        """Apply a click from the to-do list: store a typed answer on the company
        profile, then mark the task done and pay its seed-cash reward."""
        if not action:
            return
        kind, task = action[0], action[1]
        if kind == "answer" and task.field:
            self._set_company_field(task.field, action[2])
        if self.taskboard.complete(task.key):
            self.cash += task.reward
            self.link.save_tasks(self.taskboard.done)

    def _refresh_tasks(self) -> None:
        """Auto-complete any to-do whose condition now holds (a role hired, a
        building leased, the team grown) and persist if anything newly ticked."""
        leased = {b.id for b in self.park.buildings if b.status in ("hq", "leased")}
        stats = {
            "roles": {a.role for a in self.all_agents},
            "agents": len(self.all_agents),
            "leased": leased,
        }
        # Company decisions drive the name/pitch auto-ticks; narrative beats
        # (cofounder/website/analytics) still come from the CEO profile.
        if self.company.get("name"):
            stats["company_name"] = self.company["name"]
        if self.company.get("pitch"):
            stats["pitch"] = self.company["pitch"]
        if self.ceo_profile:
            for k in ("company_name", "pitch", "cofounder", "website", "analytics"):
                if self.ceo_profile.get(k):
                    stats.setdefault(k, self.ceo_profile[k])
        if self.taskboard.refresh(stats):
            self.link.save_tasks(self.taskboard.done)

    def _apply_ceo_profile(self, p: dict) -> None:
        """Stamp a saved/just-chosen CEO profile onto the player character."""
        ceo = self.player.ch
        ceo.name = p.get("name") or "You (CEO)"
        # Guard old saves: a prior bug stored the *business model* answer under "model"
        # (now "business_model"), which is the avatar gltf — fall back if it's not one.
        model = p.get("model") or config.CEO_MODEL
        ceo.model = model if model.endswith((".gltf", ".glb")) else config.CEO_MODEL
        ceo.skin_tone = roster.tone_color(p.get("skin_idx", 0))
        ceo.hair_tone = roster.palette_color(config.HAIR_COLORS, p.get("hair_idx", 0))
        ceo.outfit_tone = roster.palette_color(config.SUIT_COLORS, p.get("suit_idx", 0))
        ceo.eye_tone = roster.palette_color(config.EYE_COLORS, p.get("eye_idx", 0))
        ceo.hair_style = p.get("hair_style", 0)
        # The company profile is the source of truth for the name; only fall back to
        # the prologue's company_name when the profile hasn't got one yet.
        if not self.company.get("name") and p.get("company_name"):
            self.company_name = p["company_name"]

    def _unlock_outfit(self, outfit_id: str, price: int) -> bool:
        """Buy a premium outfit once: charge, remember it, persist. Returns True if
        already owned or successfully purchased; False if the player can't afford it.
        Shared by the CEO builder (suit styles) and the marketplace (model outfits)."""
        if outfit_id in self.unlocked:
            return True
        if self.cash < price:
            return False
        self.cash -= price
        self.unlocked.add(outfit_id)
        self.link.save_unlocks(self.unlocked)
        return True

    def _open_onboarding(self, to_park: bool = False) -> None:
        """Re-open the CEO creator (Settings). `to_park` controls whether confirm
        drops the CEO into the park or just resumes where they are."""
        self.onboarding.open_with(self.ceo_profile)
        self._onboarding_to_park = to_park
        self.onboarding_active = True

    # -- roster ---------------------------------------------------------------
    def _spawn_agent(self, *, name: str, role: str, dept: str, color, backend_id: str,
                     home_room: str, skin_tone=None, model: str | None = None,
                     appearance: dict | None = None) -> Character:
        """Create an agent in the MASTER roster, assigned to a home room (a wing).
        Its desk/position are set when that room becomes active (see _show_room).

        `appearance` (the hire-time look, also restored from the DB) tints hair/eyes/
        suit and sets the hairstyle so a customized hire keeps its look."""
        idx = len(self.all_agents)
        if appearance and skin_tone is None:
            skin_tone = roster.tone_color(appearance.get("skin_idx", 0))
        agent = Character(
            name=name, role=role, dept=dept, x=0.0, z=0.0,
            color=color, yaw=0.0, skin_tone=skin_tone,
            model=model or config.AGENT_MODELS[idx % len(config.AGENT_MODELS)],
            desk=None, backend_id=backend_id, home_room=home_room,
        )
        if appearance:
            agent.hair_tone = roster.palette_color(config.HAIR_COLORS, appearance.get("hair_idx", 0))
            agent.eye_tone = roster.palette_color(config.EYE_COLORS, appearance.get("eye_idx", 0))
            agent.outfit_tone = roster.palette_color(config.SUIT_COLORS, appearance.get("suit_idx", 0))
            agent.hair_style = appearance.get("hair_style", 0)
        agent.brain = BotBrain(agent, default_policy(agent, (0.0, 0.0)), self.bot_ctx)
        self.all_agents.append(agent)
        self.all_brains.append(agent.brain)
        self.used_names.add(name)
        return agent

    def _show_room(self, room_key: str) -> None:
        """Make the agents homed in `room_key` the active set: seat them at this
        room's desks and swap them into the live lists (mutated in place so
        bot_ctx / Director / the meeting panel keep working). Lobbies show none."""
        plan = self.plan
        members = [a for a in self.all_agents if a.home_room == room_key]
        self.agents[:] = members
        self.brains[:] = [a.brain for a in members]
        self.characters[:] = [self.player.ch] + members
        cap = plan.desk_capacity()
        for i, a in enumerate(members):
            a.desk = plan.grid_to_world(*plan.desk_slot(i)) if i < cap else None
            b = a.brain
            b.cmd, b.state, b._seated, b._route_i = None, "work", False, 0
            b.follower.clear()
            names = plan.zone_names()
            b.policy.route = [z for z in b.policy.route if z in names] or names[:3]
        self.selected = -1

    def _rebuild_nav(self) -> None:
        """(Re)build the navigation grid from current desks + furniture, then
        recompute seating. Cheap and rare — only on launch, a hire (new desk), or
        a furniture purchase."""
        desks = [a.desk for a in self.agents if a.desk is not None]
        nav = navgrid.build(desks, self.scene.furniture(), self.plan.cols, self.plan.rows)
        for mc in zones.meeting_centers():           # block every conference table
            nav.block_aabb(*mc, 0.6, 0.6)
        self.bot_ctx.nav = nav

        # Each desk's chair sits just in front of the desk, snapped to a walkable
        # cell; that's the bot's work seat (and WORK home). Idle workers are moved
        # onto it so they line up with the rendered chair.
        for a in self.agents:
            if a.desk is None or a.brain is None:
                continue
            seat = nav.snap_point(a.desk[0], a.desk[1] + 0.5)
            a.seat = seat
            a.brain.policy.home = seat
            b = a.brain
            if b.state == "work" and not b.follower.active and not b.commanded:
                a.x, a.z = seat

        # Seatable zones: each lounge couch (sit on it, facing out, +z => yaw 0).
        seats: dict = {}
        for z in self.plan.zones_of("lounge"):
            lx, lz = self.plan.grid_to_world(z.col, z.row)
            seats[z.name] = ((lx, lz + 0.1), 0.0)
        self.bot_ctx.seats = seats

    # -- movement-policy authoring (LLM, off-thread) --------------------------
    def _apply_policy(self, brain, data: dict) -> None:
        """Merge an authored policy dict into a live BotPolicy (keeps `home`)."""
        if brain is None or not data:
            return
        p = brain.policy
        for knob in ("sociability", "restlessness", "focus"):
            if knob in data:
                setattr(p, knob, float(data[knob]))
        if data.get("route"):
            p.route = list(data["route"])
            brain._route_i = 0
        if data.get("banter"):
            p.banter = list(data["banter"])
        if data.get("mood"):
            p.mood = str(data["mood"])

    def _pump_policies(self) -> None:
        """Each frame: apply any ready authored policy; otherwise request one for
        an un-planned agent, rate-limited by the Director's LLM budget."""
        for brain in self.brains:
            aid = brain.ch.backend_id
            if not aid:
                continue
            ready = self.link.poll_policy(aid)
            if ready is not None:
                self._apply_policy(brain, ready)
                self.link.store.set_policy(aid, json.dumps(ready))
                self._planned.add(aid)
            elif (aid not in self._planned and not self.link.is_planning(aid)
                  and self.director.can_spend_llm()):
                if self.link.request_policy(aid, role=brain.ch.role, name=brain.ch.name,
                                            zone_names=zones.all_names(),
                                            context=self._policy_context()):
                    self._planned.add(aid)        # mark requested; poll still applies it
                    self.director.note_llm_spend()  # consume budget at request time

    def _policy_context(self) -> str:
        """A short line of company state to flavor authored routes/banter — now
        seeded with what the company actually does so behaviour fits the mission."""
        bits = [f"{len(self.agents)} people on the team at {self.company_name}"]
        if self.company.get("pitch"):
            bits.append(self.company["pitch"])
        if self.company.get("customer"):
            bits.append(f"serving {self.company['customer']}")
        return ". ".join(bits) + "."

    # -- natural-language commands (issued in the chat) -----------------------
    def handle_chat_command(self, agent: Character, text: str) -> str | None:
        """If `text` is a movement command, apply it to the bot(s) and return an
        ack (which closes the chat); otherwise None so it flows to the model."""
        if agent is None or agent.brain is None:
            return None
        intent = commands.parse(text, agent, self.agents)
        if intent is None:
            return None
        if intent.all_bots:
            for brain in self.brains:
                brain.command("gather", target=intent.target)
        else:
            agent.brain.command(intent.kind, target=intent.target)
        agent.brain.say(intent.ack)                 # show the ack as a world bubble
        if agent.backend_id:                        # record the exchange in history
            self.link.store.add_message(agent.backend_id, "human", text)
            self.link.store.add_message(agent.backend_id, "ai", intent.ack)
        return intent.ack

    # -- physical meetings: gather bots at the table while a meeting runs ------
    def _sync_meeting_gather(self) -> None:
        """When a backend meeting is running, pull its members to the meeting table;
        release them back to their desks when it ends."""
        active = self.meeting_link.running()
        if active and not self._meeting_active:
            self._gather_for_meeting(list(self.meeting_link.members.keys()))
            self._meeting_active = True
        elif not active and self._meeting_active:
            for brain in self._gathered:
                brain.command("back_to_work")        # USER prio clears the MEETING hold
            self._gathered = []
            self._meeting_active = False

    def _gather_for_meeting(self, member_ids: list[str]) -> None:
        """Seat each meeting member on a stool, filling every meeting table in the
        plan; overflow (more members than stools) stands in an outer ring."""
        center = zones.meeting_center()
        seats = [s for mc in zones.meeting_centers() for s in zones.meeting_seats(mc)]
        ids = set(member_ids)
        brains = [a.brain for a in self.agents
                  if a.backend_id in ids and a.brain is not None]
        self._gathered = brains
        extra = max(1, len(brains) - len(seats))
        for i, brain in enumerate(brains):
            if i < len(seats):
                brain.command("gather", target=seats[i], priority=P_MEETING, sit=True)
            else:                                  # overflow: stand farther out
                ang = 2.0 * math.pi * (i - len(seats)) / extra
                spot = (center[0] + math.cos(ang) * 2.3, center[1] + math.sin(ang) * 2.3)
                brain.command("gather", target=spot, priority=P_MEETING, sit=False)

    def _restore_agents(self) -> None:
        """Re-create saved agents into the master roster, keeping each one's saved
        home wing if it's still valid; _assign_rooms fills any that aren't."""
        valid = set(self.interior.rooms)
        for i, row in enumerate(self.link.roster()):
            if i >= self.max_desks:
                break
            home = row.home_room if row.home_room in valid else None
            look = None
            if row.char_appearance:        # restore the look customized at hire time
                try:
                    look = json.loads(row.char_appearance)
                except (ValueError, TypeError):
                    look = None
            agent = self._spawn_agent(
                name=row.name, role=row.role, dept=row.dept,
                color=ROLES[i % len(ROLES)][1], backend_id=row.id,
                home_room=home,
                model=row.char_model,   # restore the avatar chosen at hire (None -> cycled default)
                appearance=look,
            )
            # Reuse a previously authored policy if we have one — no model call.
            if row.policy:
                try:
                    self._apply_policy(agent.brain, json.loads(row.policy))
                    self._planned.add(row.id)
                except (ValueError, TypeError):
                    pass
        self._assign_rooms()

    # -- department -> wing assignment ----------------------------------------
    _RECEPTION_KEYS = ("reception", "front desk", "concierge", "receptionist")

    def _assign_rooms(self) -> None:
        """Spread the roster across the building's wings by department (each dept
        gets a wing; teammates stay together). Reception-type roles go to the
        lobby. Keeps any still-valid assignment and persists the result."""
        wings = self.interior.wings()
        if not wings:
            return
        valid = set(self.interior.rooms)
        lobby = self.interior.rooms.get(self.interior.entry_room)
        reception_key = lobby.key if (lobby and lobby.reception) else None

        # Seed dept->wing from agents already validly placed in a wing.
        dept_wing: dict[str, str] = {}
        for a in self.all_agents:
            if a.home_room in valid and self.interior.rooms[a.home_room].kind == "wing":
                dept_wing.setdefault(self._dept_key(a), a.home_room)

        nxt = len(dept_wing)
        for a in self.all_agents:
            if a.home_room in valid:
                continue                          # keep a good existing assignment
            d = self._dept_key(a)
            if reception_key and any(k in d for k in self._RECEPTION_KEYS):
                a.home_room = reception_key
            else:
                if d not in dept_wing:
                    dept_wing[d] = wings[nxt % len(wings)]
                    nxt += 1
                a.home_room = dept_wing[d]
        for a in self.all_agents:                 # persist
            if a.home_room and a.backend_id:
                self.link.store.set_home_room(a.backend_id, a.home_room)

    # Legacy data uses role strings ("Engineer") and dept strings ("Engineering")
    # interchangeably; canonicalize so the same team lands in one wing.
    _DEPT_ALIASES = {
        "engineer": "engineering", "eng": "engineering", "dev": "engineering",
        "developer": "engineering", "researcher": "research",
        "designer": "design", "marketer": "marketing", "marketing lead": "marketing",
        "analyst": "analytics", "sales rep": "sales", "recruiter": "recruiting",
    }

    @classmethod
    def _dept_key(cls, a) -> str:
        d = (a.dept or a.role or "general").strip().lower()
        return cls._DEPT_ALIASES.get(d, d)

    def cycle_selection(self, step: int) -> None:
        """Move the agent selection by ±1 (D-pad / Tab). No-op with no agents."""
        if not self.agents:
            self.selected = -1
            return
        self.selected = (self.selected + step) % len(self.agents)

    @property
    def max_desks(self) -> int:
        """Total headcount cap: base desks plus a bump per office leased, capped by
        the current building's total wing desk capacity (across all its wings)."""
        leased = sum(1 for b in self.park.buildings if b.status == "leased")
        cap = BASE_DESKS + leased * DESKS_PER_LEASE
        wing_seats = sum(self.interior.rooms[k].plan(self.plans).desk_capacity()
                         for k in self.interior.wings()) or BASE_DESKS
        return min(wing_seats, cap)

    @property
    def has_desk_space(self) -> bool:
        return len(self.all_agents) < self.max_desks

    @property
    def can_hire(self) -> bool:
        return self.cash >= config.HIRE_COST and self.has_desk_space

    def _open_market(self) -> None:
        """Open the agent marketplace (browsing is free; cash is gated per pick)."""
        if not self.has_desk_space or self.market.open or self.hire_dialog.open:
            return
        self.market.open_()

    def _pick_character(self, item: dict) -> None:
        """A character was chosen in the marketplace -> set up the hire candidate
        (model + price) and open the role/skin dialog to finish."""
        self.market.close()
        cand = roster.generate(len(self.all_agents), self.used_names)
        cand["model"] = item["model"]
        cand["cost"] = item["price"]
        cand["char_name"] = item["name"]
        self.hire_dialog.open_for(cand)

    def _commit_hire(self, cand: dict, appearance: dict) -> None:
        """Hire the confirmed candidate: persist to SQL, add to the roster, charge.
        If you're standing in a wing the hire joins it; otherwise they go to their
        department's wing (see _assign_rooms). `appearance` is the look chosen in the
        hire dialog (skin/hair/hairstyle/eyes) — persisted so it survives a restart."""
        cost = cand.get("cost", config.HIRE_COST) if cand else 0
        if cand is None or not self.has_desk_space or self.cash < cost:
            return
        home = self.room.key if self.room.kind == "wing" else None
        backend_id = self.link.hire(cand["name"], cand["role"], cand["dept"],
                                    char_model=cand.get("model"),
                                    char_appearance=json.dumps(appearance))
        self._spawn_agent(
            name=cand["name"], role=cand["role"], dept=cand["dept"],
            color=cand["color"], backend_id=backend_id, home_room=home,
            model=cand.get("model"), appearance=appearance,
        )
        self.cash -= cost
        self._assign_rooms()             # dept-place the hire if no wing was active; persist
        self._show_room(self.room.key)   # re-seat the active room (picks up a hire here)
        self._rebuild_nav()

    # -- shop -----------------------------------------------------------------
    def buy_item(self, item: dict) -> None:
        """Charge for a shop item: paint recolors the room, furniture is placed."""
        if self.cash < item["price"]:
            return
        self.cash -= item["price"]
        kind = item["kind"]
        if kind == "floor_paint":
            self.scene.set_floor_color(item["color"])
        elif kind == "wall_paint":
            self.scene.set_wall_color(item["color"])
        elif kind == "door_paint":
            self.scene.set_door_color(item["color"])
        else:
            ceo = self.player.ch
            yaw = math.radians(ceo.yaw)
            x = ceo.x + math.sin(yaw) * 1.7        # a step in front of the player
            z = ceo.z + math.cos(yaw) * 1.7
            hx = config.GRID_COLS * config.TILE / 2.0 - 0.8
            hz = config.GRID_ROWS * config.TILE / 2.0 - 0.8
            x, z = max(-hx, min(hx, x)), max(-hz, min(hz, z))
            self._buy_seq += 1
            rng = random.Random(config.FURNITURE_SEED * 31 + self._buy_seq)
            self.scene.add_prop(furniture.build(kind, rng, x, z, item.get("params")))
            self._rebuild_nav()   # new prop becomes an obstacle for pathfinding

    # -- interaction ----------------------------------------------------------
    def _nearest_agent(self) -> Character | None:
        ceo = self.player.ch
        best, best_d = None, TALK_RANGE
        for a in self.agents:
            d = math.hypot(a.x - ceo.x, a.z - ceo.z)
            if d < best_d:
                best, best_d = a, d
        return best

    def _talk_target(self) -> Character | None:
        """The selected agent, else the nearest one within talking range."""
        if 0 <= self.selected < len(self.agents):
            return self.agents[self.selected]
        return self._nearest_agent()

    def _freeze_chat_target(self, target: Character) -> None:
        """Hold a bot still (and facing the CEO) for the duration of a chat."""
        self._chatting = target
        if target.brain is not None:
            target.brain.frozen = True
            target.brain.follower.clear()
        ceo = self.player.ch
        target.yaw = math.degrees(math.atan2(ceo.x - target.x, ceo.z - target.z))

    def _pick_agent(self) -> int:
        """Index of the agent under the mouse cursor, or -1. Ray-casts into 3D."""
        get_ray = getattr(pr, "get_screen_to_world_ray", None) or pr.get_mouse_ray
        ray = get_ray(pr.get_mouse_position(), self.camera.camera)
        best_i, best_d = -1, float("inf")
        for i, a in enumerate(self.agents):
            h = a.height
            box = pr.BoundingBox(pr.Vector3(a.x - 0.4, a.y, a.z - 0.4),
                                 pr.Vector3(a.x + 0.4, a.y + h, a.z + 0.4))
            hit = pr.get_ray_collision_box(ray, box)
            if hit.hit and hit.distance < best_d:
                best_i, best_d = i, hit.distance
        return best_i

    # -- office park ----------------------------------------------------------
    def _enter_park(self) -> None:
        self.mode = "park"
        self.selected = -1
        self._e_cooldown = 2       # don't let an exiting E press re-enter a building
        locomotion.set_bounds(*self.park.bounds)
        ceo = self.player.ch
        ceo.x, ceo.z = self.park.spawn
        ceo.y, ceo.yaw = 0.0, 0.0       # face downtown / HQ (toward +z)
        # Point the camera the same way (behind the CEO, looking at HQ), else it
        # spawns facing away and HQ is off-screen behind you.
        self.camera.yaw = math.radians(180.0)
        self.camera.pitch = math.radians(20.0)
        self.camera.distance = 10.0

    def _enter_office(self, building=None) -> None:
        # Always arrive in the building's lobby (then ride up to the wings). A new
        # building builds its interior first.
        if building is not None and building is not self.current_building:
            self.current_building = building
            self.interior = interior.for_building(building, self.plans)
        self.mode = "office"
        lobby = self.interior.entry_room
        ent = self.interior.rooms[lobby].plan(self.plans).point("entrance")
        self._activate_room(lobby, entry=ent)
        self.player.ch.y, self.player.ch.yaw = 0.0, 0.0
        self._office_spawn = (self.player.ch.x, self.player.ch.z)
        self._e_cooldown = 2       # don't let the entering E press fire a portal next frame

    # -- portals (move between rooms) -----------------------------------------
    PORTAL_REACH = 2.4

    def _nearest_portal(self):
        ceo = self.player.ch
        best, best_d = None, self.PORTAL_REACH
        for p in self.room.portals:
            d = math.hypot(p.pos[0] - ceo.x, p.pos[1] - ceo.z)
            if d < best_d:
                best, best_d = p, d
        return best

    def _use_portal(self, p) -> None:
        if p.kind == interior.EXIT:
            self._enter_park()
        elif p.kind == interior.ELEVATOR:
            self.elevator_open = True
        elif p.kind == interior.DOORWAY and p.to in self.interior.rooms:
            self._activate_room(p.to, entry=p.entry)

    def _activate_room(self, room_key: str, entry: tuple | None = None) -> None:
        """Switch the active interior to room `room_key`: redraw it, swap in the
        agents that live there, rebuild nav, and place the CEO at `entry`."""
        self.room = self.interior.rooms[room_key]
        self.plan = self.room.plan(self.plans)
        zones.set_active(self.plan)
        self.scene.set_plan(self.plan, seed=self.room.seed)
        locomotion.set_bounds(*self.plan.bounds())
        self._meeting_active, self._gathered = False, []   # meetings don't span rooms
        self._show_room(room_key)          # seat this room's agents (mutates live lists)
        self._rebuild_nav()
        for a in self.agents:              # deskless overflow: park by the meeting table
            if a.desk is None:
                a.seat = None
                a.x, a.z = self.plan.primary_meeting()
        ceo = self.player.ch
        ceo.x, ceo.z = entry if entry is not None else \
            self.plan.grid_to_world(self.plan.cols / 2 - 0.5, self.plan.rows - 2)

    def _visit_quest_stop(self, npc) -> None:
        """Press-E at a quest-stop NPC building. Picks its next unfinished to-do (a
        workshop like the Incubator steps through several); if that to-do asks for a
        decision, open a text field to capture it — so the answer reaches the agents'
        brains. Otherwise complete it outright."""
        if npc.task == "raise_round":          # the VC firm: an investor meeting, not a to-do
            self.investor.open_panel()
            self._e_cooldown = 8
            return
        pending = npc.pending(self.taskboard.done)
        if not pending:
            self.inbox.post(npc.name, "Already taken care of — nothing else to do here.",
                            kind="system", subject=npc.name, ts=pr.get_time())
            self._e_cooldown = 8
            return
        self._open_quest_task(npc, pending[0])

    def _open_quest_task(self, npc, key: str) -> None:
        """Begin one of a quest stop's to-dos: capture text if it asks, else finish it."""
        task = tasks.TASK_BY_KEY.get(key)
        if task is not None and task.ask and task.field:
            self._quest_input, self._quest_task, self._quest_buf = npc, key, ""
            while pr.get_char_pressed() > 0:               # drop the 'e' that opened it
                pass
        else:
            self._complete_quest_stop(npc, key)

    def _complete_quest_stop(self, npc, key: str, answer: str | None = None) -> None:
        """Mark one of a quest stop's to-dos done, store any typed decision on the
        company profile, pay the reward, and post the flavor note. No double reward."""
        task = tasks.TASK_BY_KEY.get(key)
        if answer and task is not None and task.field:
            self._set_company_field(task.field, answer)
        if self.taskboard.complete(key):
            self.link.save_tasks(self.taskboard.done)
            reward = npc.reward or (task.reward if task else 0)   # building flat fee, else per-block
            if reward:
                self.cash += reward
            msg = npc.blurb or f"Done at {npc.name}."
            if answer:
                msg += f'  You told them: "{answer}".'
            if reward:
                msg += f"  (+${reward:,} seed money)"
            self.inbox.post(npc.name, msg, kind="system",
                            subject=f"✓ {task.title}" if task else npc.name, ts=pr.get_time())
        self._e_cooldown = 8            # swallow the same E so it doesn't re-fire

    def _draw_quest_input(self) -> None:
        """Modal text field for a quest stop that asks for a company decision. Saves
        on Enter (-> _complete_quest_stop), cancels on Esc. Mirrors the to-do input."""
        npc = self._quest_input
        key = self._quest_task
        task = tasks.TASK_BY_KEY[key]
        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        w, h = 600, 248
        x, y = (sw - w) // 2, (sh - h) // 2
        pr.draw_rectangle(0, 0, sw, sh, pr.Color(0, 0, 0, 160))
        pr.draw_rectangle(x, y, w, h, pr.Color(26, 30, 42, 255))
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 2, pr.Color(90, 210, 230, 255))
        # speaker tab + the NPC's spoken line, so it reads like talking to someone.
        # The coffee meeting is with Robin in person, not "the cafe".
        speaker = COFOUNDER_NAME if key == "cofounder" else npc.name
        pr.draw_rectangle(x, y - 32, max(200, pr.measure_text(speaker, 20) + 28), 32,
                          pr.Color(90, 210, 230, 255))
        pr.draw_text(speaker, x + 16, y - 26, 20, pr.Color(12, 24, 30, 255))
        # Workshop progress (e.g. the canvas): show how many blocks remain.
        keys = npc.task_keys()
        if len(keys) > 1:
            step = sum(1 for k in keys if self.taskboard.is_done(k)) + 1
            prog = f"{step}/{len(keys)}"
            pr.draw_text(prog, x + w - pr.measure_text(prog, 18) - 16, y - 26, 18,
                         pr.Color(12, 24, 30, 255))
        line = QUEST_LINES.get(key, "Let's get this sorted — fill it in for me.")
        ly = y + 18
        cur = ""
        for word in line.split(" "):
            trial = (cur + " " + word).strip()
            if pr.measure_text(trial, 20) > w - 40 and cur:
                pr.draw_text(cur, x + 20, ly, 20, pr.RAYWHITE)
                cur, ly = word, ly + 26
            else:
                cur = trial
        if cur:
            pr.draw_text(cur, x + 20, ly, 20, pr.RAYWHITE)
        pr.draw_text(task.ask.upper(), x + 20, y + h - 92, 14, pr.Color(120, 215, 235, 255))
        field = pr.Rectangle(x + 20, y + h - 72, w - 40, 40)
        pr.draw_rectangle_rec(field, pr.Color(14, 16, 24, 255))
        pr.draw_rectangle_lines_ex(field, 1, pr.Color(90, 210, 230, 255))
        ch = pr.get_char_pressed()
        while ch > 0:
            if 32 <= ch < 127 and len(self._quest_buf) < 60:
                self._quest_buf += chr(ch)
            ch = pr.get_char_pressed()
        bs = pr.is_key_pressed(pr.KEY_BACKSPACE)
        if hasattr(pr, "is_key_pressed_repeat"):
            bs = bs or pr.is_key_pressed_repeat(pr.KEY_BACKSPACE)
        if bs and self._quest_buf:
            self._quest_buf = self._quest_buf[:-1]
        caret = "_" if (pr.get_time() % 1.0) < 0.5 else ""
        shown = (self._quest_buf + caret) if self._quest_buf else ("type here" + caret)
        pr.draw_text(shown, int(field.x) + 10, int(field.y) + 10, 20,
                     pr.GOLD if self._quest_buf else pr.Color(110, 120, 140, 255))
        pr.draw_text("Enter to save   ·   Esc to cancel", x + 20, y + h - 24, 14,
                     pr.Color(150, 160, 180, 255))
        if pr.is_key_pressed(pr.KEY_ESCAPE):
            self._quest_input, self._quest_task, self._quest_buf = None, None, ""
        elif pr.is_key_pressed(pr.KEY_ENTER) and self._quest_buf.strip():
            self._complete_quest_stop(npc, key, self._quest_buf.strip())
            # Workshop flow: roll straight on to the next unfilled block; otherwise close.
            nxt = next((k for k in npc.pending(self.taskboard.done)
                        if tasks.TASK_BY_KEY.get(k) and tasks.TASK_BY_KEY[k].ask), None)
            if nxt is not None:
                self._quest_task, self._quest_buf = nxt, ""
            else:
                self._quest_input, self._quest_task, self._quest_buf = None, None, ""

    def _park_frame(self, dt: float) -> None:
        """One frame of the walkable park: move, lease/enter, draw."""
        self.park.update(dt)          # advance ambient city traffic
        ceo = self.player.ch
        # Freeze the world while a to-do/quest text field, the dossier, the phone, or
        # an investor meeting is open.
        frozen = (self.todo.capturing or self._quest_input is not None
                  or self.dossier.open or self.investor.open or self.phone.open)
        if self.phone.open:                    # the Nokia works out in the city too
            self.phone.update()
        if not frozen:
            self.player.update(dt, self.camera)
            ceo.x, ceo.z = self.park.collide(ceo.x, ceo.z)   # block walking through buildings
            self.camera.update(dt, self.player.ch)
            ceo.update(dt, self.registry)
        self.pedestrians.update(dt, ceo.x, ceo.z, self.registry)  # ambient crowd
        near = self.park.nearest(ceo.x, ceo.z)
        # Quest-stop NPC buildings (Chamber of Commerce, …) are only offered when no
        # lease lot is in reach, so E is never ambiguous (they never share a corner).
        near_npc = self.park.nearest_npc(ceo.x, ceo.z) if near is None else None

        if not frozen:
            if pr.is_key_pressed(pr.KEY_P):
                self._enter_office(); return
            if pr.is_key_pressed(pr.KEY_O):
                self._open_onboarding(to_park=False); return
            press_e = self._e_cooldown == 0 \
                and (pr.is_key_pressed(pr.KEY_E) or gamepad.pressed(gamepad.TRIANGLE))
            if near is not None and press_e:
                if near.leased:
                    self._enter_office(near); return
                elif self.cash >= near.deposit:
                    self.cash -= near.deposit
                    self.park.lease(near)        # capacity rises via max_desks
            elif near_npc is not None and press_e:
                self._visit_quest_stop(near_npc)

        pr.begin_drawing()
        pr.clear_background(self.daylight.sky_color())
        self.park.draw(self.camera.camera, self.season.name, self.taskboard.done)
        pr.begin_mode_3d(self.camera.camera)
        self.pedestrians.draw(self.registry)
        ceo.draw(self.registry)
        pr.end_mode_3d()
        self._draw_park_overlay(near, near_npc)
        todo.draw_objective(self.taskboard)
        self._do_todo_action(self.todo.draw(self.taskboard))
        if self._quest_input is not None:        # quest-stop decision capture, on top
            self._draw_quest_input()
        self._do_dossier_action(self.dossier.draw(self.company))
        self._do_investor_action(self.investor.draw(self.company, self._rounds_raised()))
        if self.phone.open:                    # Nokia overlay, on top of the city
            self.phone.draw()
        pr.end_drawing()

    def _label_3d(self, text, sub, sub_color, wx, wy, wz, font=16, main_color=None) -> None:
        """Draw a floating world-space label, culling labels behind the camera or
        off-screen (which is what made distant headers scatter)."""
        cam = self.camera.camera
        fx, fz = cam.target.x - cam.position.x, cam.target.z - cam.position.z
        if (wx - cam.position.x) * fx + (wz - cam.position.z) * fz <= 0:
            return                                    # behind the camera
        sp = pr.get_world_to_screen(pr.Vector3(wx, wy, wz), cam)
        if sp.x < 0 or sp.x > config.WINDOW_WIDTH:
            return
        # clamp into view so labels on very tall buildings still show (pinned just
        # under the HUD bar) instead of vanishing off the top.
        sy = min(max(int(sp.y), 64), config.WINDOW_HEIGHT - 44)
        sx = int(sp.x)
        nw = pr.measure_text(text, font)
        pr.draw_rectangle(sx - nw // 2 - 6, sy - 3, nw + 12,
                          38 if sub else 22, pr.Color(0, 0, 0, 160))
        pr.draw_text(text, sx - nw // 2, sy, font, main_color or pr.RAYWHITE)
        if sub:
            sw = pr.measure_text(sub, 13)
            pr.draw_text(sub, sx - sw // 2, sy + 18, 13, sub_color)

    def _draw_clock(self) -> None:
        """Top-center chip naming the current time-of-day phase and season."""
        label = f"{self.daylight.phase_name}  ·  {self.season.name}"
        w = pr.measure_text(label, 20)
        x = config.WINDOW_WIDTH // 2 - w // 2
        pr.draw_rectangle(x - 12, 12, w + 24, 30, pr.Color(20, 24, 34, 200))
        pr.draw_text(label, x, 16, 20, pr.Color(235, 220, 170, 255))

    def _draw_park_overlay(self, near, near_npc=None) -> None:
        ceo = self.player.ch
        LABEL_DIST = 32.0     # only name buildings near the CEO (HQ is always shown)
        # Lease lots: label when near; HQ always (so you can find your way home).
        for b in self.park.buildings:
            d = math.hypot(b.x - ceo.x, b.z - ceo.z)
            if d > LABEL_DIST and b.status != "hq":
                continue
            if b.status == "available":
                sub, col, mc = f"LEASE  ${b.deposit:,}", pr.Color(255, 170, 90, 255), None
            else:
                sub, col, mc = b.dept, pr.Color(150, 230, 175, 255), pr.Color(255, 214, 110, 255)
            # cap label height so tall towers don't pin their label onto the HUD
            ly = min(self.park.top_of(b), 6.5)
            self._label_3d(b.name, sub, col, b.x, ly, b.z, 16, main_color=mc)

        # NPC shops: only the ones near you. Quest stops get a to-do sub-line (or a
        # green ✓ once done) so it's clear which buildings advance the quest log.
        for n in self.park.npc:
            if math.hypot(n.x - ceo.x, n.z - ceo.z) > LABEL_DIST:
                continue
            sub, sub_col = None, None
            if n.is_quest_stop:
                keys = n.task_keys()
                pending = n.pending(self.taskboard.done)
                if not pending:
                    sub, sub_col = "✓ Done", pr.Color(120, 220, 150, 255)
                elif len(keys) > 1:           # workshop (the canvas): show progress
                    sub = f"TO-DO: Business Model Canvas ({len(keys) - len(pending)}/{len(keys)})"
                    sub_col = pr.Color(120, 215, 235, 255)
                else:
                    sub = f"TO-DO: {tasks.TASK_BY_KEY[pending[0]].title}"
                    sub_col = pr.Color(120, 215, 235, 255)
            self._label_3d(n.name, sub, sub_col, n.x, min(self.park.top_of(n), 6.0), n.z, 14,
                           main_color=pr.Color(240, 226, 180, 255))

        # top bar: cash + rent ledger
        pr.draw_rectangle(0, 0, config.WINDOW_WIDTH, 56, pr.Color(20, 24, 34, 230))
        pr.draw_text("Office Park", 18, 14, 28, pr.RAYWHITE)
        ceo = self.player.ch
        addr = parkmod.address_label(ceo.x, ceo.z)
        aw = pr.measure_text(addr, 18)
        pr.draw_rectangle(14, 60, aw + 20, 26, pr.Color(20, 24, 34, 210))
        pr.draw_text(addr, 24, 64, 18, pr.Color(150, 200, 235, 255))
        pr.draw_text(f"Cash: ${self.cash:,}", 360, 18, 22, pr.GOLD)
        rent = self.park.monthly_rent()
        pr.draw_text(f"Rent: ${rent:,}/mo", 600, 18, 22,
                     pr.Color(230, 150, 150, 255) if rent else pr.LIGHTGRAY)
        # rent-due progress bar
        pr.draw_rectangle(600, 44, 200, 6, pr.Color(60, 64, 76, 255))
        pr.draw_rectangle(600, 44, int(200 * self.park.rent_progress()), 6, pr.Color(210, 130, 130, 255))
        self._draw_clock()

        # interaction prompt — lease lots take priority, else a quest-stop offer.
        prompt, afford = None, True
        if near is not None:
            if near.leased:
                prompt = f"Press  E  to enter {near.name}"
            else:
                prompt = f"Press  E  to lease {near.name}   -   Deposit ${near.deposit:,}  ·  Rent ${near.rent:,}/mo"
                afford = self.cash >= near.deposit
        elif near_npc is not None:
            pending = near_npc.pending(self.taskboard.done)
            if not pending:
                prompt = f"{near_npc.name} — nothing left to do here"
            elif len(near_npc.task_keys()) > 1:        # workshop: the canvas
                prompt = (f"Press  E  at {near_npc.name}  to work your business model "
                          f"canvas   -   {len(pending)} left")
            else:
                task = tasks.TASK_BY_KEY[pending[0]]
                bonus = f"   -   +${near_npc.reward:,} seed money" if near_npc.reward else ""
                prompt = f"Press  E  at {near_npc.name}  to {task.title.lower()}{bonus}"
        if prompt is not None:
            tw = pr.measure_text(prompt, 20)
            x = (config.WINDOW_WIDTH - tw) // 2
            y = config.WINDOW_HEIGHT - 70
            pr.draw_rectangle(x - 14, y - 8, tw + 28, 36, pr.Color(0, 0, 0, 170))
            pr.draw_text(prompt, x, y, 20, pr.RAYWHITE if afford else pr.Color(230, 140, 140, 255))
        pr.draw_text("WASD move  -  E lease / enter  -  P office  -  O edit CEO  -  "
                     "L to-dos  -  C company  -  N phone",
                     18, config.WINDOW_HEIGHT - 28, 18, pr.LIGHTGRAY)

    def run(self) -> None:
        pr.set_config_flags(pr.FLAG_MSAA_4X_HINT)
        pr.init_window(config.WINDOW_WIDTH, config.WINDOW_HEIGHT, config.WINDOW_TITLE)
        pr.set_target_fps(config.TARGET_FPS)
        pr.set_exit_key(pr.KEY_NULL)   # Esc must NOT quit the game; it only closes the chat
        hire_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 90, 210, 56, "")
        shop_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 154, 210, 50, "Shop  (B)")
        meeting_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 218, 210, 50, "Meeting  (G)")
        park_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 282, 210, 50, "Office Park  (P)")
        settings_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 346, 210, 50, "Edit CEO  (O)")
        files_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 410, 210, 50, "Files  (V)")
        jobs_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 474, 210, 50, "Jobs  (J)")
        phone_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 538, 210, 50, "Phone  (N)")

        while not pr.window_should_close():
            dt = pr.get_frame_time()

            # Home screen: New World (wipe + prologue) / Continue (resume) / Quit.
            if self.menu_active:
                choice = self.menu.draw(self.ceo_profile is not None)
                if choice == "continue":
                    self.menu_active = False
                elif choice == "new":
                    self.menu_active = False
                    self._new_world()
                elif choice == "quit":
                    break
                continue

            # First-launch story prologue: owns the whole frame until the founder is
            # created and the company named/pitched, then fades into the new city.
            if self.prologue_active:
                result = self.prologue.draw(self.registry)
                if result is not None:
                    self._apply_ceo_profile(result)
                    self.link.save_ceo(result)
                    self.ceo_profile = result
                    self.prologue_active = False
                    self.prologue.dispose()
                    self._enter_park()       # the gate already faded; arrive in the city
                continue

            # First-launch CEO-creation tutorial: owns the whole frame until the
            # player confirms, then saves the profile and drops them in the park.
            if self.onboarding_active:
                result = self.onboarding.draw(self.registry, self.unlocked,
                                              self.cash, self._unlock_outfit)
                if result == "cancel":           # re-edit backed out: discard, resume
                    self.onboarding_active = False
                    self.onboarding.dispose()
                elif result is not None:
                    self._apply_ceo_profile(result)
                    self.link.save_ceo(result)
                    self.ceo_profile = result
                    self.onboarding_active = False
                    self.onboarding.dispose()    # free the preview render texture
                    if self._onboarding_to_park:
                        self._enter_park()
                continue

            # Rent accrues no matter where the CEO is standing.
            self.cash -= self.park.tick_rent(dt)

            # Advance the day/night cycle and feed it to the character shader (the
            # sky color is read per-frame in each draw path). T peeks at the next
            # phase without waiting for the clock.
            self.daylight.advance(dt)
            if pr.is_key_pressed(pr.KEY_T):
                self.daylight.skip_phase()
            self.registry.set_daylight(self.daylight)

            # Seasons drift much slower than the day; trees swap foliage as it turns.
            self.season.advance(dt)

            # Release agents whose background reply has landed, even if their chat
            # panel is closed — otherwise they stay stuck showing "working".
            self._reconcile_busy_agents()

            # Ambient inbox: agents drop the odd status update, park businesses
            # (NPCs) reach out. Real "finished work" messages come from above.
            self.inbox_feeder.tick(dt, self.all_agents,
                                   [n.name for n in self.park.npc],
                                   self.inbox, pr.get_time())

            if self._e_cooldown > 0:        # swallow an E press that lingers across
                self._e_cooldown -= 1       # a mode/room switch (no EndDrawing between)

            self._refresh_tasks()           # tick the plot's auto-completing to-dos
            # L toggles the to-do list anywhere except while a text field has focus.
            if pr.is_key_pressed(pr.KEY_L) and not self.chat.open and not self.todo.capturing:
                self.todo.toggle()
            # C toggles the Company Dossier (view/edit the decisions agents read).
            if pr.is_key_pressed(pr.KEY_C) and not self.chat.open and not self.dossier.capturing:
                self.dossier.toggle()
            # N toggles the Nokia phone from anywhere (office OR city) — press again to
            # close. Skipped while a text field owns the keyboard (incl. the phone's own
            # message screens, where N should type a letter, not slam the phone shut).
            if pr.is_key_pressed(pr.KEY_N) and not self.chat.open \
                    and not self.todo.capturing and not self.dossier.capturing \
                    and not self.phone.capturing and self._quest_input is None:
                if self.phone.open:
                    self.phone.close()
                else:
                    self.phone.open_panel()

            if self.mode == "park":
                self._park_frame(dt)
                continue

            if self.chat.open:
                # Chat captures the keyboard; freeze movement/camera/hiring.
                self.chat.update()
            elif self.phone.open:
                self.phone.update()
            elif self.meeting.open:
                self.meeting.update()
            elif self.drive.open:
                self.drive.update()
            elif self.jobs.open:
                self.jobs.update()
            elif self.elevator_open or self.hire_dialog.open or self.shop.open or self.market.open:
                pass  # the modal handles its own input inside draw()
            elif self.todo.open or self.dossier.open:
                pass  # a full-screen panel is up; it's modal — freeze movement/keys
            else:
                self.player.update(dt, self.camera, self.characters)
                self.camera.update(dt, self.player.ch)
                if gamepad.pressed(gamepad.DPAD_RIGHT) or pr.is_key_pressed(pr.KEY_TAB):
                    self.cycle_selection(1)
                elif gamepad.pressed(gamepad.DPAD_LEFT):
                    self.cycle_selection(-1)
                if pr.is_key_pressed(pr.KEY_B) or gamepad.pressed(gamepad.DPAD_UP):
                    self.shop.open_()
                if pr.is_key_pressed(pr.KEY_M) and self.has_desk_space:
                    self._open_market()
                if pr.is_key_pressed(pr.KEY_G) and len(self.agents) >= 2:
                    self.meeting.open_panel()
                if pr.is_key_pressed(pr.KEY_P):
                    self._enter_park()
                if pr.is_key_pressed(pr.KEY_V):
                    self.drive.open_panel()
                if pr.is_key_pressed(pr.KEY_J):
                    self.jobs.open_panel()
                if pr.is_key_pressed(pr.KEY_O):
                    self._open_onboarding(to_park=False)
                    continue
                # Left-click an agent to select it (ignore clicks on the HUD buttons).
                m = pr.get_mouse_position()
                if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT) \
                        and not pr.check_collision_point_rec(m, hire_btn.rect) \
                        and not pr.check_collision_point_rec(m, shop_btn.rect) \
                        and not pr.check_collision_point_rec(m, meeting_btn.rect) \
                        and not pr.check_collision_point_rec(m, park_btn.rect) \
                        and not pr.check_collision_point_rec(m, files_btn.rect) \
                        and not pr.check_collision_point_rec(m, jobs_btn.rect) \
                        and not pr.check_collision_point_rec(m, phone_btn.rect) \
                        and not pr.check_collision_point_rec(m, settings_btn.rect):
                    picked = self._pick_agent()
                    if picked >= 0:
                        self.selected = picked
                target = self._talk_target()
                if target and (pr.is_key_pressed(pr.KEY_F) or gamepad.pressed(gamepad.TRIANGLE)):
                    self.chat.open_with(target)
                    self._freeze_chat_target(target)
                # Walk up to a portal and press E to use it (doorway / elevator / exit).
                portal = self._nearest_portal()
                if portal is not None and pr.is_key_pressed(pr.KEY_E) and self._e_cooldown == 0:
                    self._use_portal(portal)

            # Release the held bot once its conversation closes.
            if self._chatting is not None and not self.chat.open:
                if self._chatting.brain is not None:
                    self._chatting.brain.frozen = False
                self._chatting = None

            # Autonomous bot life runs every frame, even while a modal is open, so
            # the office keeps moving while the CEO chats, shops, or hires.
            if self.bot_ctx.nav is not None:
                self._pump_policies()       # author/apply LLM policies (rate-limited)
                self._sync_meeting_gather()  # pull bots to the table during a meeting
                self.director.tick(dt)
                for brain in self.brains:
                    brain.update(dt)

            for ch in self.characters:
                ch.update(dt, self.registry)

            pr.begin_drawing()
            pr.clear_background(self.daylight.sky_color())  # time-of-day sky

            sel = self.agents[self.selected] if 0 <= self.selected < len(self.agents) else None
            self.scene.draw_world(self.characters, self.registry, self.camera.camera, sel)
            self._draw_portals_3d(self.camera.camera)
            draw_world_labels(self.characters, self.camera.camera)
            self._draw_agent_status()
            self._draw_meeting_badges()
            self._draw_bubbles()
            self._draw_clock()

            if self.chat.open:
                self.chat.draw()
            elif self.phone.open:
                self.phone.draw()
            elif self.meeting.open:
                self.meeting.draw()
            elif self.drive.open:
                self.drive.draw()
            elif self.jobs.open:
                self.jobs.draw()
            elif self.elevator_open:
                self._elevator_frame()
            elif self.hire_dialog.open:
                result = self.hire_dialog.draw()
                if result == "hire":
                    self._commit_hire(self.hire_dialog.candidate,
                                      self.hire_dialog.appearance())
                    self.hire_dialog.close()
                elif result == "cancel":
                    self.hire_dialog.close()
            elif self.shop.open:
                action = self.shop.draw(self.cash)
                if action == "close":
                    self.shop.close()
                elif isinstance(action, tuple) and action[0] == "buy":
                    self.buy_item(action[1])
            elif self.market.open:
                action = self.market.draw(self.cash, self.unlocked)
                if action == "close":
                    self.market.close()
                elif isinstance(action, tuple) and action[0] == "unlock":
                    self._unlock_outfit(action[1]["id"], action[1]["unlock"])
                elif isinstance(action, tuple) and action[0] == "hire":
                    self._pick_character(action[1])
            else:
                draw_hud(self.company_name, self.cash, len(self.agents), config.HIRE_COST, sel)
                if self.has_desk_space:
                    hire_btn.label, hire_btn.enabled = "Hire Agent  (M)", True
                else:
                    hire_btn.label, hire_btn.enabled = "Office Full", False
                # Open the marketplace to browse characters; the hire finishes in the dialog.
                if hire_btn.draw() or (self.has_desk_space and gamepad.pressed(gamepad.SQUARE)):
                    self._open_market()
                if shop_btn.draw():
                    self.shop.open_()
                meeting_btn.enabled = len(self.agents) >= 2
                if meeting_btn.draw():
                    self.meeting.open_panel()
                if park_btn.draw():
                    self._enter_park()
                if files_btn.draw():
                    self.drive.open_panel()
                if jobs_btn.draw():
                    self.jobs.open_panel()
                unread = self.inbox.unread()
                phone_btn.label = f"Phone  (N)   {unread} new" if unread else "Phone  (N)"
                if phone_btn.draw():
                    self.phone.open_panel()
                if settings_btn.draw():
                    self._open_onboarding(to_park=False)
                self._draw_room_label()
                self._draw_talk_prompt()
                portal = self._nearest_portal()
                if portal is not None:
                    self._draw_portal_prompt(portal)
                todo.draw_objective(self.taskboard)
                self._do_todo_action(self.todo.draw(self.taskboard))
                self._do_dossier_action(self.dossier.draw(self.company))

            pr.end_drawing()

        self.chat.voice.shutdown()
        self.meeting_link.shutdown()
        self.coordinator.shutdown()
        self.link.shutdown()
        self.park.unload()
        self.registry.unload_all()
        pr.close_window()

    def _reconcile_busy_agents(self) -> None:
        """Flip any 'working' agent back to idle once its reply lands.

        The chat panel only polls the agent it's open on, so a reply that
        finishes while the panel is closed (the CEO walked away) would otherwise
        never be consumed and the bot would show "working" forever. We drain
        every *other* agent's finished reply here; poll_reply returns None unless
        a pending job is actually done, so idle agents are left untouched. The
        reply is already persisted to the store, and now also lands in the phone
        inbox so the CEO sees the agent finished while they were away.
        """
        open_id = self.chat.agent.backend_id if self.chat.open else None
        phone_id = self.phone.active_agent_id      # phone may own an agent reply too
        for a in self.all_agents:
            if not a.backend_id or a.backend_id in (open_id, phone_id):
                continue   # no job, or an open panel owns this one
            reply = self.link.poll_reply(a.backend_id)
            if reply is not None:
                a.status = "idle"
                if not reply.startswith("[error"):
                    self.inbox.post(a.name, reply, kind="agent",
                                    subject=f"Finished: {_inbox_short(reply, 26)}",
                                    agent_id=a.backend_id, ts=pr.get_time())

    def _draw_agent_status(self) -> None:
        """Float a 'working…' badge above any agent currently busy on a reply."""
        dots = "." * (1 + int(pr.get_time() * 2) % 3)
        for a in self.agents:
            if a.status != "working":
                continue
            if a.brain is not None and a.brain.state == "meeting":
                continue   # the 'in a meeting' badge takes precedence
            anchor = pr.Vector3(a.x, a.y + a.height + 1.1, a.z)
            sp = pr.get_world_to_screen(anchor, self.camera.camera)
            text = "working" + dots
            tw = pr.measure_text(text, 16)
            x, y = int(sp.x - tw / 2), int(sp.y)
            pr.draw_rectangle(x - 6, y - 3, tw + 12, 22, pr.Color(150, 90, 20, 210))
            pr.draw_text(text, x, y, 16, pr.Color(255, 222, 150, 255))

    def _draw_meeting_badges(self) -> None:
        """Float an 'in a meeting' badge above any bot gathered at the table."""
        for a in self.agents:
            brain = a.brain
            if brain is None or brain.state != "meeting":
                continue
            anchor = pr.Vector3(a.x, a.y + a.height + 1.1, a.z)
            sp = pr.get_world_to_screen(anchor, self.camera.camera)
            text = "in a meeting"
            tw = pr.measure_text(text, 16)
            x, y = int(sp.x - tw / 2), int(sp.y)
            pr.draw_rectangle(x - 6, y - 3, tw + 12, 22, pr.Color(120, 70, 160, 215))
            pr.draw_text(text, x, y, 16, pr.Color(235, 215, 250, 255))

    def _draw_bubbles(self) -> None:
        """Float each bot's current speech bubble (banter / command ack) above it."""
        for a in self.agents:
            brain = a.brain
            if brain is None or not brain.bubble:
                continue
            anchor = pr.Vector3(a.x, a.y + a.height + 0.7, a.z)
            sp = pr.get_world_to_screen(anchor, self.camera.camera)
            text = brain.bubble
            tw = pr.measure_text(text, 16)
            x, y = int(sp.x - tw / 2), int(sp.y)
            pr.draw_rectangle(x - 8, y - 5, tw + 16, 26, pr.Color(255, 255, 255, 230))
            pr.draw_rectangle_lines(x - 8, y - 5, tw + 16, 26, pr.Color(120, 130, 150, 255))
            pr.draw_text(text, x, y, 16, pr.Color(40, 44, 55, 255))

    def _draw_talk_prompt(self) -> None:
        target = self._talk_target()
        if target is None:
            return
        text = f"Press  F / △  to talk to {target.name}"
        tw = pr.measure_text(text, 20)
        x = (config.WINDOW_WIDTH - tw) // 2
        y = config.WINDOW_HEIGHT - 96
        pr.draw_rectangle(x - 12, y - 6, tw + 24, 32, pr.Color(0, 0, 0, 150))
        pr.draw_text(text, x, y, 20, pr.RAYWHITE)

    # -- portals: 3D markers, prompt, elevator menu ---------------------------
    _PORTAL_COLOR = {interior.ELEVATOR: pr.Color(90, 150, 230, 255),
                     interior.DOORWAY: pr.Color(90, 200, 130, 255),
                     interior.EXIT: pr.Color(230, 160, 80, 255)}

    def _draw_portals_3d(self, camera) -> None:
        """A small lit pad + arch at each portal so you can see where to go."""
        pr.begin_mode_3d(camera)
        for p in self.room.portals:
            x, z = p.pos
            col = self._PORTAL_COLOR.get(p.kind, pr.RAYWHITE)
            pr.draw_cylinder(pr.Vector3(x, 0.02, z), 0.55, 0.55, 0.04, 16,
                             pr.Color(col.r, col.g, col.b, 120))
            for ox in (-0.5, 0.5):                       # two posts
                pr.draw_cube(pr.Vector3(x + ox, 1.05, z), 0.12, 2.1, 0.12, col)
            pr.draw_cube(pr.Vector3(x, 2.15, z), 1.24, 0.16, 0.16, col)  # lintel
        pr.end_mode_3d()

    def _draw_room_label(self) -> None:
        """Top-center banner telling you which building + room you're in."""
        room = f" — {self.room.label}" if self.room.label else ""
        txt = f"{self.current_building.name}{room}"
        tw = pr.measure_text(txt, 18)
        x = (config.WINDOW_WIDTH - tw) // 2
        pr.draw_rectangle(x - 12, 12, tw + 24, 26, pr.Color(20, 24, 34, 205))
        pr.draw_text(txt, x, 16, 18, pr.Color(150, 200, 235, 255))

    def _draw_portal_prompt(self, portal) -> None:
        verb = {interior.ELEVATOR: "take the elevator",
                interior.EXIT: "exit to the park"}.get(portal.kind,
                                                       f"go to {portal.label}")
        text = f"Press  E  to {verb}"
        tw = pr.measure_text(text, 20)
        x = (config.WINDOW_WIDTH - tw) // 2
        pr.draw_rectangle(x - 12, config.WINDOW_HEIGHT - 132, tw + 24, 32, pr.Color(0, 0, 0, 160))
        pr.draw_text(text, x, config.WINDOW_HEIGHT - 126, 20, pr.RAYWHITE)

    def _elevator_frame(self) -> None:
        """Draw the floor picker and handle its input. Click a floor (or press its
        number) to ride there; Esc closes."""
        menu = self.interior.floor_menu(self.plans)
        w, rh = 320, 40
        h = 70 + rh * len(menu)
        x = (config.WINDOW_WIDTH - w) // 2
        y = (config.WINDOW_HEIGHT - h) // 2
        pr.draw_rectangle(0, 0, config.WINDOW_WIDTH, config.WINDOW_HEIGHT, pr.Color(0, 0, 0, 120))
        pr.draw_rectangle(x, y, w, h, pr.Color(22, 26, 38, 245))
        pr.draw_rectangle(x, y, w, 44, pr.Color(90, 150, 230, 255))
        pr.draw_text("Elevator — pick a floor", x + 16, y + 12, 20, pr.RAYWHITE)
        mouse = pr.get_mouse_position()
        pick = None
        for i, (level, label, key, entry) in enumerate(menu):
            ry = y + 56 + i * rh
            row = pr.Rectangle(x + 12, ry, w - 24, rh - 6)
            here = key == self.room.key
            hover = pr.check_collision_point_rec(mouse, row)
            pr.draw_rectangle_rec(row, pr.Color(54, 60, 78, 255) if hover else
                                  pr.Color(36, 40, 54, 255))
            tag = f"  {i + 1}.  {label}" + ("   (you are here)" if here else "")
            pr.draw_text(tag, int(row.x) + 8, int(ry) + 7, 18,
                         pr.Color(150, 200, 235, 255) if here else pr.RAYWHITE)
            if (hover and pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)) or \
               pr.is_key_pressed(ord(str(min(i + 1, 9)))):
                pick = (key, entry)
        pr.draw_text("Esc to cancel", x + 16, y + h - 22, 14, pr.LIGHTGRAY)
        if pr.is_key_pressed(pr.KEY_ESCAPE):
            self.elevator_open = False
        elif pick is not None:
            self.elevator_open = False
            self._activate_room(pick[0], entry=pick[1])


if __name__ == "__main__":
    Game().run()

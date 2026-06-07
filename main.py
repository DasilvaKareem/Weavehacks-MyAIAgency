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
import queue
import random
import re
from types import SimpleNamespace

# Print a real Python->C stack to stderr if a native call (raylib/GL) segfaults,
# so the occasional hard crash leaves a trace instead of just dying silently.
faulthandler.enable()

import pyray as pr

from game import config, gamepad, roster, furniture, navgrid, locomotion, zones, commands, floorplan, interior, daylight, season, calendar, tasks, dialogue, voice
from game import park as parkmod
from game.park import Park, load_lots as load_park
from game.shop import ShopPanel, load_catalog
from game.marketplace import load_catalog as load_agents
from game.assets import ModelRegistry
from game.scene import Scene
from game.camera import ThirdPersonCamera
from game.player import Player
from game.ui import Button, draw_hud, draw_world_labels
from game.entities import Character, make_ceo
from game.onboarding import OnboardingScreen
from game.dossier_panel import DossierPanel
from game.investor_panel import InvestorPanel
from game import market as market_mod
from game import farm as farm_mod
from game.market_panel import MarketPanel
from game.slot_panel import SlotPanel
from game.farm_panel import FarmPanel
from game.grant_panel import GrantPanel
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

# What characters say at each quest stop now lives in assets/dialogue.json (a beat
# per task key, one or more lines), loaded into Game.dialogue. See game/dialogue.py.

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
        self.calendar = calendar.GameCalendar()  # in-game date, advanced by the day/night loop
        self.plans = floorplan.load_plans()

        self.mode = "office"
        self.park = Park(load_park())
        self.pedestrians = Pedestrians()              # ambient sidewalk crowd (park)
        # Robin, your co-founder-to-be: stands a few steps ahead of the park spawn
        # with a "!" overhead, so your very first move is to walk up and pitch them
        # (the coffee meeting). Built once here; reused as the actor inside the cafe
        # quest building (see _spawn_quest_actor).
        self.robin = Character(name=COFOUNDER_NAME, role="Co-founder", x=0.0, z=0.0,
                               color=pr.Color(90, 210, 230, 255), dept="Founder",
                               model="Suit_Male.gltf", yaw=180.0)
        # Give the named NPCs a real appearance, else the model's raw "Skin"
        # material (~black) shows. Fixed looks so they're recognizable each run.
        roster.apply_look(self.robin, {"skin_idx": 2, "hair_idx": 2, "eye_idx": 1, "suit_idx": 2})
        # Once you've won Robin over, you can tell him (Nokia → Co-founder → "Follow
        # me") to walk with you — trailing you through the office AND out in the park.
        # Toggled from the phone; the per-frame trailing lives in _update_companion.
        self.robin_following = False
        self._robin_voice = voice.pick_voice(COFOUNDER_NAME)   # he acks the toggle aloud
        # Your first intern: hangs out in a city park (Founders Green) with a "!"
        # overhead. Walk up + E in the park to take them on — they join the company
        # for free. Positioned/bound to its park in _enter_park; gated on the
        # "intern" quest so they vanish once hired (and stay gone after a restart).
        self.intern = Character(name="Eager Intern", role="Intern", x=0.0, z=0.0,
                                color=pr.Color(150, 210, 120, 255), dept="Operations",
                                model="Casual_Male.gltf", yaw=0.0)
        roster.apply_look(self.intern, {"skin_idx": 4, "hair_idx": 0, "eye_idx": 1})
        self._intern_park = None       # bound to a GreenSpace in _enter_park
        # Bob, your childhood friend: waiting right where you spawn the first time you
        # arrive in the city, with a "!" overhead. Walk up + E and he welcomes you back
        # and presses $10,000 of seed money into your hand — a one-time gift, gated on
        # the persistent `bob_gift` flag so he's gone afterward (and stays gone after a
        # restart). _bob_done is loaded from the store once the link exists.
        self.bob = Character(name="Bob", role="Old Friend", x=0.0, z=0.0,
                             color=pr.Color(225, 170, 120, 255), dept="Friend",
                             model="Casual2_Male.gltf", yaw=180.0)
        roster.apply_look(self.bob, {"skin_idx": 2, "hair_idx": 2, "hair_style": 1, "eye_idx": 2})
        self._bob_voice = voice.pick_voice("Bob")
        self._bob_done = False         # set from link.load_flag("bob_gift") below
        self._bob_talk = None          # current line index while chatting, else None
        self._bob_mode = "welcome"     # which conversation is active: welcome | rescue
        # Emergency rescue: when you go broke, Bob texts you to meet at a park for a
        # one-time $2,500. Both flags load from the store once the link exists.
        self._bob_rescue_done = False      # already claimed the bailout?
        self._bob_rescue_pending = False   # texted you, waiting at the park?
        # Mae, the small-business desk: stands in a city park. Talk to her (walk up +
        # E) to unlock the affordable Starter Office (simple lobby + one wing, no
        # elevator, $200/mo). Gated on the persistent `starter_office` flag.
        self.lady = Character(name="Mae", role="Small-Biz Desk", x=0.0, z=0.0,
                              color=pr.Color(210, 150, 190, 255), dept="Civic",
                              model="Suit_Female.gltf", yaw=180.0)
        roster.apply_look(self.lady, {"skin_idx": 3, "hair_idx": 3, "hair_style": 2, "eye_idx": 1})
        self._lady_voice = voice.pick_voice("Mae")
        self._starter_unlocked = False     # set from link.load_flag below
        self._lady_talk = None             # current line index while chatting, else None
        self._park_toast = ""              # transient park message (e.g. "locked — meet Mae")
        self._park_toast_until = 0.0
        # To-Do guide: pick a to-do on the Nokia and the city points the way to where
        # it gets done (gold beacon over the building + an on-screen arrow). Holds the
        # selected task key; the target world spot is re-resolved live each frame.
        self._guide_key = None
        # Civilian side-quest: Walter's lost pug, Biscuit. Talk to Walter in a park,
        # go find Biscuit in another park, bring him back for a cash reward. Walter is
        # human (apply_look); Biscuit is an animal model (its own fur material).
        self.civilian = Character(name="Walter", role="Resident", x=0.0, z=0.0,
                                  color=pr.Color(200, 200, 120, 255), dept="Civic",
                                  model="Casual3_Male.gltf", yaw=180.0)
        roster.apply_look(self.civilian, {"skin_idx": 1, "hair_idx": 5, "hair_style": 0, "eye_idx": 0})
        self.pet = Character(name="Biscuit", role="Pug", x=0.0, z=0.0,
                             color=pr.Color(210, 180, 140, 255), dept="", model="Pug.gltf")
        self._civilian_voice = voice.pick_voice("Walter")
        self._pet_done = False             # set from link.load_flag("pet_quest") below
        self._pet_stage = 0                # 0 not started · 1 searching · 2 returning · 3 done
        self._civilian_talk = None         # current line index while chatting, else None
        # Civilian side-quest 2: Río the busker's stolen guitar. Talk to Río in a park,
        # find the guitar (a drawn prop) stashed in another park, return it for $2,500.
        self.busker = Character(name="Río", role="Busker", x=0.0, z=0.0,
                                color=pr.Color(120, 180, 210, 255), dept="Civic",
                                model="Casual2_Female.gltf", yaw=180.0)
        roster.apply_look(self.busker, {"skin_idx": 5, "hair_idx": 1, "hair_style": 3, "eye_idx": 2})
        self._busker_voice = voice.pick_voice("Río")
        self._guitar_done = False          # set from link.load_flag("guitar_quest") below
        self._guitar_stage = 0             # 0 offer · 1 searching · 2 returning · 3 done
        self._busker_talk = None           # current line index while chatting, else None
        self._guitar_pos = (0.0, 0.0)      # world (x,z) of the stashed guitar (set in _enter_park)
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
        # Characters are the single source of truth; each carries its own .brain,
        # so there are no parallel brain-lists to keep in sync. bot_ctx/Director/
        # meeting hold `self.agents` by reference, so it's mutated in place
        # (clear+extend) on a room switch, never reassigned.
        self.all_agents: list[Character] = []
        self.characters: list[Character] = [ceo]
        self.agents: list[Character] = []
        self.player = Player(ceo)
        self.camera = ThirdPersonCamera((ceo.x, ceo.y, ceo.z))
        self.selected = -1  # index into self.agents; -1 = nothing selected
        self._office_spawn = (ceo.x, ceo.z)

        # Backend: SQL persistence + one-on-one chat, off the render thread.
        self.link = CompanyLink()
        # Restore the saved bank balance + leased offices (None/empty on first run, so
        # a fresh game keeps STARTING_CASH and no offices). Autosaved in the loop and
        # forced on quit; see _persist_state.
        saved_cash = self.link.load_cash()
        if saved_cash is not None:
            self.cash = saved_cash
        saved_leases = self.link.load_leases()
        for b in self.park.buildings:
            if b.id in saved_leases:
                self.park.lease(b)
        self.calendar.load_state(self.link.load_calendar())  # restore the in-game date
        self.season.set_day(self.calendar.day)               # foliage matches the restored date
        self._bob_done = self.link.load_flag("bob_gift")   # already got the welcome gift?
        self._bob_rescue_done = self.link.load_flag("bob_rescue")
        self._bob_rescue_pending = self.link.load_flag("bob_rescue_pending")
        self._starter_unlocked = self.link.load_flag("starter_office")
        starter = next((b for b in self.park.buildings if b.id == "starter"), None)
        if starter is not None and self._starter_unlocked:
            starter.locked = False                  # already met Mae: lease it freely
        self._pet_done = self.link.load_flag("pet_quest")   # already returned Biscuit?
        self._guitar_done = self.link.load_flag("guitar_quest")   # already returned the guitar?
        self._last_persist = 0.0
        self._saved_cash = int(self.cash)
        self._saved_day = self.calendar.day
        self._saved_leases = {b.id for b in self.park.buildings if b.status == "leased"}
        # Purchased outfit ids (premium marketplace models + premium CEO suits). One
        # unlock makes that outfit reusable for free on any CEO/agent; persists.
        self.unlocked = self.link.load_unlocks()
        # The plot: a to-do list of company-building tasks, most auto-completing as
        # you play (hire a role, lease a building, grow the team). Progress persists.
        # The only place it's shown is the Nokia phone's To-Do screen (N → To-Do);
        # there's deliberately no on-screen panel or HUD chip for it.
        self.taskboard = tasks.TaskBoard(self.link.load_tasks())
        # Mirror the Good-Deeds side-quest flags onto the To-Do board so finished ones
        # show ✓ after a restart (the flags are the source of truth; see _sync_deeds).
        self._sync_deeds()
        self.dialogue = dialogue.load()    # NPC story-beat lines (assets/dialogue.json)
        self.dossier = DossierPanel()      # view/edit the company decisions agents read
        self.investor = InvestorPanel()    # pitch the VC for a funding round
        self.market = market_mod.Market.load(self.link)   # idle stock-market state
        self.farm = farm_mod.Farm.load(self.link)         # idle South-America farm
        self.market_panel = MarketPanel()  # bank/broker trading terminal
        self.slot_panel = SlotPanel()      # Lucky's Casino slot machine
        self.farm_panel = FarmPanel()      # Trade Embassy idle farm
        self.grant_panel = GrantPanel()    # Grants Office: LLM-judged funding
        self.chat = ChatPanel(self.link)
        self.shop = ShopPanel(load_catalog())
        self.hire_catalog = load_agents()          # marketplace models for the phone Hire app
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
        # Task firehose: the phone's Tasks app enqueues; this in-game Dispatcher
        # pulls tasks off the Redis queue and runs them on idle agents (cap-bounded),
        # posting each result to the inbox. Results land on a thread-safe queue and
        # are drained in the game loop (_reconcile_busy_agents) to stay off-thread.
        self._task_results: "queue.Queue" = queue.Queue()
        self._dispatcher = None
        try:
            from backend import task_queue as _task_queue
            self._dispatcher = _task_queue.Dispatcher(
                store=self.link.store,
                on_result=lambda t, r, a: self._task_results.put((t, r, a)))
            self._dispatcher.start()
        except Exception as exc:   # no Redis / backend issue — Tasks app degrades gracefully
            print(f"[task firehose] dispatcher not started: {exc}")
        # The Nokia command center: text the co-founder or any agent, read the
        # inbox, hire from the marketplace (the Upwork-style Hire app). Contacts are
        # the whole roster (any room). The hire bridge hands the phone just the few
        # callables it needs, so PhonePanel stays decoupled from the Game.
        hire_bridge = SimpleNamespace(
            new_candidate=self._new_candidate,       # () -> fresh random candidate dict
            departments=roster.departments,          # () -> [(dept, [(title, color, rate)])]
            role_rate=roster.role_rate,              # (title) -> int
            roles=lambda: [t for t, _, _ in roster.ROLES],   # kept for parity
            cash=lambda: int(self.cash),
            can_hire=lambda: self.has_desk_space,
            hire=self._hire_candidate,               # (candidate, role_title) -> bool
            is_hr=lambda role: role in self._HR_ROLES,
            hire_by_text=self._hr_hire_from_text,    # (text) -> ack str | None
        )
        # Co-founder follow toggle, handed to the phone the same decoupled way as the
        # hire bridge: the phone flips the flag, the game does the walking.
        follow_bridge = SimpleNamespace(
            is_following=lambda: self.robin_following,
            set_following=self._set_robin_following,
        )
        # City map: the phone projects these world POIs onto its LCD. `here` is the
        # "you are here" pin (your park position in the city, or the building you're
        # standing in when you're indoors).
        citymap_bridge = SimpleNamespace(
            here=self._map_here,
            bounds=lambda: self.park.bounds,
            markers=self._map_markers,
        )
        # Clock app: the phone reads the live date + time-of-day (to the minute).
        clock_bridge = SimpleNamespace(state=self._clock_state)
        # To-do guide: the phone hands a task key to start(); the game lights up the
        # city toward where it's done and tells the phone whether to close + navigate.
        guide_bridge = SimpleNamespace(start=self._start_guide)
        self.phone = PhonePanel(self.link, self.coordinator,
                                lambda: self.all_agents, self.inbox, self.taskboard,
                                hire=hire_bridge, follow=follow_bridge,
                                citymap=citymap_bridge, clock=clock_bridge,
                                guide=guide_bridge)
        self.inbox.post("Company.AI",
                        "Welcome! Your team and the neighborhood reach you here. "
                        "Open the phone (N) and tap a message to read it.",
                        kind="system", subject="Welcome to your phone", ts=0.0)
        self._buy_seq = 0
        self.used_names: set[str] = set()

        self.bot_ctx = BotContext(nav=None, ceo=ceo, agents=self.agents)
        self.director = Director(self.agents)
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
        self._quest_line = 0            # which dialogue line of the current beat is showing
        self._quest_action = None       # store kind ("outfit"/"hire") when a greeting ends by opening a shop; None for a normal ask
        self._quest_replay = False      # revisiting a finished stop → play its `done` lines, not the original
        self._reception_greeted = False  # front-desk greeting fires once per lobby visit; re-armed on each room entry
        # Quest buildings are entered like offices: you walk into a default floor and
        # talk (E) to one NPC inside. These hold the in-visit state; None when not in one.
        self._quest_building = None     # the NpcBuilding whose interior we're inside
        self._quest_actor = None        # the Character standing in it to talk to
        self._saved_office = None       # (current_building, interior) to restore on exit
        self._park_return = None        # where you stood in the park before entering a building
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
        # New game: forget the old save snapshot so the fresh balance/leases persist.
        self._saved_cash, self._saved_leases, self._last_persist = -1, set(), 0.0
        # Bob's welcome gift + emergency rescue are available again in the new city.
        self._bob_done, self._bob_talk = False, None
        self._bob_rescue_done, self._bob_rescue_pending = False, False
        self.link.set_flag("bob_gift", False)
        self.link.set_flag("bob_rescue", False)
        self.link.set_flag("bob_rescue_pending", False)
        # Mae + the Starter Office reset too (the fresh Park relocks it from JSON).
        self._starter_unlocked, self._lady_talk = False, None
        self.link.set_flag("starter_office", False)
        # Walter's lost-pet quest resets for the new city.
        self._pet_done, self._pet_stage, self._civilian_talk = False, 0, None
        self.link.set_flag("pet_quest", False)
        # Río's stolen-guitar quest resets too.
        self._guitar_done, self._guitar_stage, self._busker_talk = False, 0, None
        self.link.set_flag("guitar_quest", False)
        self.company_name = "Company.AI"
        self.ceo_profile = None
        self.company = {}
        self._quest_input, self._quest_task, self._quest_buf = None, None, ""
        self._quest_line, self._quest_action = 0, None
        self._quest_building = self._quest_actor = self._saved_office = None
        self._park_return = None
        self.taskboard = tasks.TaskBoard(set())
        self.phone.board = self.taskboard
        self.market = market_mod.Market.fresh()       # fresh portfolio for the new run
        self.farm = farm_mod.Farm.fresh()             # fresh farm for the new run
        for lst in (self.all_agents, self.agents):
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

    def _do_market_action(self, action) -> None:
        """Apply a bank/broker trade to cash + the market (single money source), then
        persist. Buys spend cash; sells/withdrawals return it."""
        if not action:
            return
        kind = action[0]
        chunk = market_mod.BUY_CHUNK
        if kind == "buy":
            spend = min(self.cash, action[2])
            if spend > 0:
                self.cash -= self.market.buy(action[1], spend)
        elif kind == "sell":
            self.cash += self.market.sell(action[1], action[2])
        elif kind == "sellall":
            self.cash += self.market.sell_all(action[1])
        elif kind == "deposit":
            amt = min(self.cash, chunk)
            self.cash -= self.market.deposit_savings(amt)
        elif kind == "withdraw":
            self.cash += self.market.withdraw_savings(chunk)
        elif kind == "withdrawall":
            self.cash += self.market.withdraw_savings(self.market.savings)
        else:
            return
        self.market.save(self.link)

    def _do_slot_action(self, action) -> None:
        """Apply a slot-machine result to cash (single money source). The panel
        guards spins against the balance, so this only ever adds the wager back as
        a loss or credits a win."""
        if action and action[0] == "cash":
            self.cash = max(0, self.cash + action[1])

    def _do_farm_action(self, action) -> None:
        """Apply a farm action to cash (single money source): buying a plot spends
        the next-plot cost; collecting banks the accrued harvest. Persist after."""
        if not action:
            return
        kind = action[0]
        if kind == "buy":
            cost = self.farm.cost(action[1])
            if self.cash >= cost:
                self.cash -= self.farm.buy(action[1])
        elif kind == "collect":
            self.cash += self.farm.collect()
        else:
            return
        self.farm.save(self.link)

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
        roster.apply_look(ceo, p)   # skin/hair/eye/suit tone + hair_style from the profile
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
        """Re-open the CEO creator. `to_park` controls whether confirm drops the
        CEO into the park or just resumes where they are."""
        self.onboarding.open_with(self.ceo_profile)
        self._onboarding_to_park = to_park
        self.onboarding_active = True

    def _open_outfitter(self) -> None:
        """Walk into The Outfitters: open the CEO editor as the wardrobe store, so
        the player can restyle their look and buy/equip premium suits. Same screen
        as the CEO creator, just framed as a shop (OnboardingScreen.store_mode)."""
        self._open_onboarding(to_park=False)
        self.onboarding.store_mode = True
        self._e_cooldown = 8                     # swallow the E that opened the store

    def _open_hire_store(self) -> None:
        """Walk into TalentWorks Staffing: pull out the Nokia on its Hire app, so
        all hiring funnels through the one phone flow (no separate panel)."""
        self.phone.open_hire()
        self._e_cooldown = 8                     # swallow the E that opened the store

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
        self.used_names.add(name)
        return agent

    def _active_brains(self) -> list:
        """Brains of the agents on the active floor — derived from self.agents (the
        single source of truth) so there's no parallel list that can drift."""
        return [a.brain for a in self.agents if a.brain is not None]

    def _show_room(self, room_key: str) -> None:
        """Make the agents homed in `room_key` the active set: seat them at this
        room's desks and swap them into the live lists (mutated in place so
        bot_ctx / Director / the meeting panel keep working). Lobbies show none."""
        plan = self.plan
        members = [a for a in self.all_agents if a.home_room == room_key]
        self.agents[:] = members          # held by reference by bot_ctx/Director/meeting
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
        for brain in self._active_brains():
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
        # Recruiters / HR can hire for you: "hire an engineer" closes the chat with
        # a confirmation and the new agent appears, instead of going to the model.
        if agent.role in self._HR_ROLES:
            ack = self._hr_hire_from_text(text)
            if ack is not None:
                agent.brain.say(ack)
                if agent.backend_id:
                    self.link.store.add_message(agent.backend_id, "human", text)
                    self.link.store.add_message(agent.backend_id, "ai", ack)
                return ack
        intent = commands.parse(text, agent, self.agents)
        if intent is None:
            return None
        if intent.all_bots:
            for brain in self._active_brains():
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

    @classmethod
    def _is_reception(cls, a) -> bool:
        """A reception-type hire — matched on role OR dept, so a 'Receptionist'
        lands at the front desk no matter which department it's filed under
        (its dept_key is 'operations', which wouldn't match on its own)."""
        tag = f"{getattr(a, 'role', '') or ''} {getattr(a, 'dept', '') or ''}".lower()
        return any(k in tag for k in cls._RECEPTION_KEYS)

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
            if reception_key and self._is_reception(a):
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

    # Recruiters / HR can hire on your behalf when you tell them to (in 1:1 chat or
    # on the phone). Keyword → role, ordered specific-first so "devops engineer"
    # doesn't get read as a plain "engineer".
    _HR_ROLES = {"Recruiter", "Human Resources Manager"}
    _HIRE_ROLE_KEYWORDS = [
        ("Data Scientist", ("data scientist", "data science")),
        ("DevOps Engineer", ("devops", "sre", "infrastructure")),
        ("Observability Engineer", ("observability",)),
        ("Market Analyst", ("market analyst", "market research")),
        ("Research Analyst", ("research analyst", "researcher", "research")),
        ("Executive Assistant", ("executive assistant", "assistant")),
        ("Document Manager", ("document manager", "document")),
        ("Sheets Analyst", ("sheets",)),
        ("Marketing Lead", ("marketer", "marketing", "growth")),
        ("Product Designer", ("designer", "design", "ux", "ui")),
        ("Animator", ("animator", "animation", "video")),
        ("Blogger", ("blogger", "writer", "content")),
        ("Sales Rep", ("sales", "seller")),
        ("Financial Analyst", ("financial analyst", "finance", "accountant")),
        ("Operations Manager", ("operations", "ops manager")),
        ("Support Specialist", ("support",)),
        ("Recruiter", ("recruiter",)),
        ("Human Resources Manager", ("hr manager", "human resources", "people ops")),
        ("Software Engineer", ("engineer", "developer", "programmer", "coder", "swe")),
    ]
    _HIRE_VERBS = ("hire", "recruit", "bring on", "bring in", "get me", "onboard",
                   "staff up", "add a", "add an", "add another", "find me",
                   "need a", "need an", "need another", "want a", "want an")

    def _parse_hire_role(self, text: str) -> str | None:
        t = text.lower()
        for role, kws in self._HIRE_ROLE_KEYWORDS:
            if any(k in t for k in kws):
                return role
        return None

    def _hr_hire_from_text(self, text: str) -> str | None:
        """Parse 'hire [n] <role>' and hire that many through the catalog. Returns
        an ack string when it WAS a hire request (so the caller replies with it),
        or None to let the message flow to the agent's model as normal chat."""
        t = text.lower()
        if not any(v in t for v in self._HIRE_VERBS):
            return None
        role = self._parse_hire_role(text)
        if role is None:
            return "Happy to staff up — which role? e.g. \"hire an engineer\"."
        m = re.search(r"\b(\d{1,2})\b", t)
        n = max(1, min(int(m.group(1)) if m else 1, 5))
        rate = roster.role_rate(role)
        hired = 0
        for _ in range(n):
            if self._hire_candidate(self._new_candidate(), role):
                hired += 1
            else:
                break
        if hired == 0:
            if not self.has_desk_space:
                return "We're out of desks — lease another building first."
            return f"Short on cash for that — a {role} runs ${rate:,}."
        plural = "s" if hired > 1 else ""
        return f"Done — brought {hired} {role}{plural} on board."

    def _new_candidate(self) -> dict:
        """A fresh, unique hire candidate for the phone Hire app: an auto-generated
        first+last name, a random (free) character model, and a random look. The
        role and its rate are chosen on the next screen — see _hire_candidate."""
        name = roster.random_name(self.used_names)
        pool = [it for it in self.hire_catalog
                if not it.get("locked") or it["id"] in self.unlocked]
        it = random.choice(pool) if pool else None
        return {
            "name": name,
            "model": it["model"] if it else None,
            "char_name": it["name"] if it else "",
            "appearance": roster.random_look(),
        }

    def _hire_candidate(self, cand: dict, role_title: str) -> bool:
        """Hire an auto-generated candidate (phone Hire app / HR agent): stamp the
        chosen role + its rate onto the candidate's name & look, and commit. Returns
        True if the hire went through (had a desk and could afford the rate)."""
        rate = roster.role_rate(role_title)
        if not self.has_desk_space or self.cash < rate:
            return False
        cand = dict(cand)                              # don't mutate the caller's dict
        for title, dept, color in roster.ROLES:        # stamp the requested role
            if title == role_title:
                cand["role"], cand["dept"], cand["color"] = title, dept, color
                break
        cand["cost"] = rate
        appearance = cand.get("appearance") or {"skin_idx": 1, "hair_idx": 0,
                                                "hair_style": 0, "eye_idx": 0}
        before = len(self.all_agents)
        self._commit_hire(cand, appearance)
        return len(self.all_agents) > before

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

    def _take_on_intern(self) -> None:
        """Take on the free park intern. They only sign on once you actually have an
        office to put them in — otherwise they tell you to come back when you've
        leased one. Free hire (cost 0); completes the "intern" quest so they're gone
        from the park afterward (and stay gone after a restart)."""
        self._e_cooldown = 8
        if self.taskboard.is_done("intern"):
            return
        name = self.intern.name
        has_office = any(b.status in ("hq", "leased") for b in self.park.buildings)
        if not has_office:
            self.inbox.post(name, "Love the energy — but I need a desk to work at. "
                            "Get yourself an office first and I'll join you, no charge.",
                            kind="system", subject="Your first intern", ts=pr.get_time())
            return
        if not self.has_desk_space:
            self.inbox.post(name, "You're out of desks — lease another wing and come "
                            "find me.", kind="system", subject="Your first intern",
                            ts=pr.get_time())
            return
        cand = roster.generate(len(self.all_agents), self.used_names)
        cand["role"], cand["dept"] = "Intern", "Operations"
        cand["model"], cand["cost"] = "Casual_Male.gltf", 0
        appearance = {"skin_idx": cand.get("tone_idx", 1), "hair_idx": 0,
                      "hair_style": 0, "eye_idx": 0}
        before = len(self.all_agents)
        self._commit_hire(cand, appearance)
        if len(self.all_agents) > before:
            self.taskboard.complete("intern")
            self.link.save_tasks(self.taskboard.done)
            self.intern.name = cand["name"]
            self.inbox.post(cand["name"], "I'm in — first one on the team! Point me at "
                            "anything and I'll get to work. (Joined for free.)",
                            kind="system", subject="✓ Take on your first intern",
                            ts=pr.get_time())

    # -- Bob, your childhood friend (welcome gift + emergency rescue) ----------
    BOB_GIFT = 10000
    BOB_LINES = [
        "Hey — look who it is! Welcome back to the city.",
        "I always knew you'd come back to build something of your own. I'm happy for you, truly.",
        "Starting out's the hard part. Here — take this. Ten thousand bucks to get you going.",
        "No, I insist. Go make it count — I've got high hopes for you.",
    ]
    # When you go flat broke, Bob texts you to meet at a park and spots you cash.
    BOB_RESCUE_GIFT = 2500
    BOB_RESCUE_PARK_ID = "maple_commons"
    BOB_RESCUE_LINES = [
        "Hey, you made it. I got worried when I heard things had gotten tight.",
        "Listen — nobody builds anything in a straight line. Being broke for a minute doesn't mean you failed.",
        "I've got you. Here's twenty-five hundred — an emergency stake, friend to friend.",
        "Pay me back by making it work. Now go — you've got this.",
    ]

    def _bob_lines(self, mode: str) -> list[str]:
        return self.BOB_RESCUE_LINES if mode == "rescue" else self.BOB_LINES

    def _talk_to_bob(self, mode: str = "welcome") -> None:
        """Begin a Bob conversation (freezes the park; stepped with E). `mode` is
        'welcome' (the spawn gift) or 'rescue' (the broke-bailout at the park)."""
        self._bob_mode = mode
        self._bob_talk = 0
        self._e_cooldown = 8
        voice.speak(self._bob_lines(mode)[0], self._bob_voice)
        while pr.get_char_pressed() > 0:       # swallow the E that opened the chat
            pass

    def _update_bob_talk(self) -> None:
        """Step through Bob's lines on E/Enter/Space; the last one hands over cash."""
        advance = (pr.is_key_pressed(pr.KEY_E) or pr.is_key_pressed(pr.KEY_ENTER)
                   or pr.is_key_pressed(pr.KEY_SPACE) or gamepad.pressed(gamepad.CROSS)
                   or gamepad.pressed(gamepad.TRIANGLE))
        if not advance:
            return
        lines = self._bob_lines(self._bob_mode)
        self._bob_talk += 1
        if self._bob_talk >= len(lines):
            self._finish_bob()
        else:
            voice.speak(lines[self._bob_talk], self._bob_voice)

    def _finish_bob(self) -> None:
        """Close the conversation, grant the gift, and lock it as claimed for good."""
        self._bob_talk = None
        self._e_cooldown = 8
        if self._bob_mode == "rescue":
            self._bob_rescue_done = True
            self._bob_rescue_pending = False
            self.cash += self.BOB_RESCUE_GIFT
            self.link.set_flag("bob_rescue", True)
            self.link.set_flag("bob_rescue_pending", False)
            self.inbox.post("Bob", f"Slipped you ${self.BOB_RESCUE_GIFT:,} — no strings, "
                            "just don't give up on this. Call me if it gets tight again.",
                            kind="system", subject=f"Emergency stake · +${self.BOB_RESCUE_GIFT:,}",
                            ts=pr.get_time())
        else:
            self._bob_done = True
            self.cash += self.BOB_GIFT
            self.link.set_flag("bob_gift", True)
            self.inbox.post("Bob", "So good to see you back in the city. I slipped you "
                            f"${self.BOB_GIFT:,} to get started — no strings. Go build "
                            "something great; I've got high hopes for you.",
                            kind="system", subject=f"An old friend · +${self.BOB_GIFT:,}",
                            ts=pr.get_time())
        self._persist_state(force=True)        # lock in the new balance now

    # -- Bob's emergency rescue (triggered when you hit $0) -------------------
    def _bob_rescue_spot(self):
        """The park where Bob waits to bail you out — a named green space, kept
        clear of the intern's lawn. Deterministic so it survives a restart."""
        parks = self.park.parks
        return (next((g for g in parks if g.id == self.BOB_RESCUE_PARK_ID), None)
                or next((g for g in parks if g.id != "founders_green"), None)
                or (parks[0] if parks else None))

    def _check_bob_rescue(self) -> None:
        """Once you've met Bob and then run flat broke, he texts you to meet up and
        spot you an emergency stake — a one-time safety net (gated so starting at $0
        before the welcome gift doesn't trip it)."""
        if (self._bob_done and not self._bob_rescue_done
                and not self._bob_rescue_pending and self.cash <= 0):
            self._trigger_bob_rescue()

    def _trigger_bob_rescue(self) -> None:
        spot = self._bob_rescue_spot()
        self._bob_rescue_pending = True
        self.link.set_flag("bob_rescue_pending", True)
        where = spot.name if spot is not None else "the park"
        self.inbox.post("Bob", "Hey — word travels. I can tell money's gotten tight, and "
                        f"that's alright. Come find me at {where} and I'll spot you "
                        f"${self.BOB_RESCUE_GIFT:,} to get back on your feet. That's what "
                        "friends are for — don't be a stranger.", kind="system",
                        subject="Bob wants to meet up", ts=pr.get_time())
        if self.mode == "park" and spot is not None:    # place him now if you're outside
            self.bob.x, self.bob.z, self.bob.y = spot.x + 2.0, spot.z + 1.0, 0.0

    def _draw_speech_box(self, name, subtitle, line, accent, action) -> None:
        """A bottom-of-screen NPC speech box: name tab, subtitle, the wrapped spoken
        line, and a right-aligned action hint. Shared by Bob and Mae."""
        W, H = config.WINDOW_WIDTH, config.WINDOW_HEIGHT
        bw, bh = min(880, W - 80), 156
        x, y = (W - bw) // 2, H - bh - 40
        pr.draw_rectangle(x, y, bw, bh, pr.Color(12, 16, 26, 235))
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, bw, bh), 2, accent)
        pr.draw_text(name, x + 22, y + 16, 22, accent)
        pr.draw_text(subtitle, x + 32 + pr.measure_text(name, 22), y + 22, 14,
                     pr.Color(150, 160, 180, 255))
        rows, cur = [], ""
        for word in line.split():
            trial = (cur + " " + word).strip()
            if pr.measure_text(trial, 22) > bw - 44:
                rows.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            rows.append(cur)
        ty = y + 54
        for row in rows[:3]:
            pr.draw_text(row, x + 22, ty, 22, pr.RAYWHITE)
            ty += 30
        label, highlight = action
        hw = pr.measure_text(label, 16)
        pr.draw_text(label, x + bw - hw - 22, y + bh - 28, 16,
                     pr.GOLD if highlight else pr.LIGHTGRAY)

    def _draw_bob_talk(self) -> None:
        """The childhood-friend speech box at the bottom of the screen."""
        lines = self._bob_lines(self._bob_mode)
        is_last = self._bob_talk == len(lines) - 1
        gift = self.BOB_RESCUE_GIFT if self._bob_mode == "rescue" else self.BOB_GIFT
        action = ((f"Press  E / X  to accept  +${gift:,}", True) if is_last
                  else ("Press  E / X  to continue", False))
        self._draw_speech_box("BOB", "your childhood friend", lines[self._bob_talk],
                              pr.Color(225, 170, 120, 255), action)

    # -- Mae, the small-business desk (unlocks the affordable Starter Office) --
    LADY_PARK_ID = "riverside_park"
    LADY_LINES = [
        "Oh — hello! You've got the look of someone about to start something. Am I right?",
        "I run the small-business desk. Most folks think you need a tower and a big deposit. You don't.",
        "There's a little place on 8th — the Starter Office. One room and a lobby, no elevator, just $200 a month.",
        "It's yours to lease whenever you're ready. Go on — every empire starts in a small room.",
    ]

    def _lady_park(self):
        """The park where Mae waits. Deterministic so she's findable across restarts."""
        parks = self.park.parks
        return (next((g for g in parks if g.id == self.LADY_PARK_ID), None)
                or next((g for g in parks if g.id not in ("founders_green", "maple_commons")), None)
                or (parks[0] if parks else None))

    def _talk_to_lady(self) -> None:
        """Begin Mae's conversation (freezes the park; stepped with E)."""
        if self._starter_unlocked:
            return
        self._lady_talk = 0
        self._e_cooldown = 8
        voice.speak(self.LADY_LINES[0], self._lady_voice)
        while pr.get_char_pressed() > 0:
            pass

    def _update_lady_talk(self) -> None:
        advance = (pr.is_key_pressed(pr.KEY_E) or pr.is_key_pressed(pr.KEY_ENTER)
                   or pr.is_key_pressed(pr.KEY_SPACE) or gamepad.pressed(gamepad.CROSS)
                   or gamepad.pressed(gamepad.TRIANGLE))
        if not advance:
            return
        self._lady_talk += 1
        if self._lady_talk >= len(self.LADY_LINES):
            self._finish_lady()
        else:
            voice.speak(self.LADY_LINES[self._lady_talk], self._lady_voice)

    def _finish_lady(self) -> None:
        """Close the chat and unlock the Starter Office for leasing, for good."""
        self._lady_talk = None
        self._e_cooldown = 8
        self._starter_unlocked = True
        self.link.set_flag("starter_office", True)
        starter = next((b for b in self.park.buildings if b.id == "starter"), None)
        if starter is not None:
            starter.locked = False
            self.inbox.post("Mae", f"Lovely to meet you! The {starter.name} on 8th is "
                            f"unlocked — a lobby and one wing, no elevator, ${starter.rent:,}/"
                            f"month (${starter.deposit:,} to move in). Walk up and press E to "
                            "sign. Find it on your phone map.", kind="system",
                            subject="✓ Starter Office unlocked", ts=pr.get_time())

    def _draw_lady_talk(self) -> None:
        is_last = self._lady_talk == len(self.LADY_LINES) - 1
        action = (("Press  E / X  to finish", True) if is_last
                  else ("Press  E / X  to continue", False))
        self._draw_speech_box("MAE", "small-business desk", self.LADY_LINES[self._lady_talk],
                              pr.Color(220, 150, 195, 255), action)

    # -- Walter's lost-pet quest (find Biscuit, return him for a reward) -------
    PET_REWARD = 2500
    PET_NAME = "Biscuit"
    CIVILIAN_PARK_ID = "liberty_square"
    PET_PARK_ID = "sunset_gardens"
    CIVILIAN_OFFER_LINES = [
        "Oh — excuse me! You look kind. I'm in a real spot here.",
        "My little pug, Biscuit, slipped his leash and bolted. Brown coat, very waggy.",
        "Someone said they saw him over by Sunset Gardens. Please — would you bring him home?",
        "Do this for me and I'll give you $2,500. He's all I've got. Thank you, truly.",
    ]
    CIVILIAN_THANKS_LINES = [
        "Biscuit! Oh — you found him! Come here, boy!",
        "I was sick with worry. I can't believe it — you actually brought him back.",
        "Here, $2,500 as promised, and then some kindness I can't repay. Bless you.",
    ]

    def _civilian_park(self):
        parks = self.park.parks
        return (next((g for g in parks if g.id == self.CIVILIAN_PARK_ID), None)
                or (parks[0] if parks else None))

    def _pet_park(self):
        parks = self.park.parks
        return (next((g for g in parks if g.id == self.PET_PARK_ID), None)
                or (parks[-1] if parks else None))

    def _civilian_lines(self):
        return self.CIVILIAN_THANKS_LINES if self._pet_stage == 2 else self.CIVILIAN_OFFER_LINES

    def _talk_to_civilian(self) -> None:
        if self._pet_done or self._pet_stage not in (0, 2):
            return
        self._civilian_talk = 0
        self._e_cooldown = 8
        voice.speak(self._civilian_lines()[0], self._civilian_voice)
        while pr.get_char_pressed() > 0:
            pass

    def _update_civilian_talk(self) -> None:
        advance = (pr.is_key_pressed(pr.KEY_E) or pr.is_key_pressed(pr.KEY_ENTER)
                   or pr.is_key_pressed(pr.KEY_SPACE) or gamepad.pressed(gamepad.CROSS)
                   or gamepad.pressed(gamepad.TRIANGLE))
        if not advance:
            return
        lines = self._civilian_lines()
        self._civilian_talk += 1
        if self._civilian_talk >= len(lines):
            self._finish_civilian()
        else:
            voice.speak(lines[self._civilian_talk], self._civilian_voice)

    def _finish_civilian(self) -> None:
        self._civilian_talk = None
        self._e_cooldown = 8
        if self._pet_stage == 0:                  # accepted the quest → go search
            self._pet_stage = 1
            where = (self._pet_park().name if self._pet_park() else "the park")
            self._toast(f"Find {self.PET_NAME} near {where}.")
            self.inbox.post("Walter", f"Thank you for helping. {self.PET_NAME} was last "
                            f"seen near {where} — bring him home and there's $"
                            f"{self.PET_REWARD:,} in it for you.", kind="system",
                            subject=f"Lost dog: {self.PET_NAME}", ts=pr.get_time())
        elif self._pet_stage == 2:                # returned the pet → reward
            self._pet_stage = 3
            self._pet_done = True
            self.cash += self.PET_REWARD
            self.link.set_flag("pet_quest", True)
            self._persist_state(force=True)
            self.inbox.post("Walter", f"You brought {self.PET_NAME} home safe. Here's $"
                            f"{self.PET_REWARD:,}, with my deepest thanks. The whole "
                            "neighborhood owes you one.", kind="system",
                            subject=f"✓ Reunited {self.PET_NAME} · +${self.PET_REWARD:,}",
                            ts=pr.get_time())

    def _find_pet(self) -> None:
        """Walk up to the lost pug: he perks up and starts following you home."""
        if self._pet_stage != 1:
            return
        self._pet_stage = 2
        self._e_cooldown = 8
        ceo = self.player.ch
        self.pet.x, self.pet.z = ceo.x + 1.0, ceo.z - 1.0   # trot to your side
        self._toast(f"You found {self.PET_NAME}! Take him home to Walter.")
        voice.speak("Yip! Yip!", self._civilian_voice)

    def _update_pet_follow(self, dt: float) -> None:
        """Biscuit trails the CEO (direct steering, like Robin — no navgrid)."""
        ceo = self.player.ch
        dx, dz = ceo.x - self.pet.x, ceo.z - self.pet.z
        dist = math.hypot(dx, dz)
        gap = 1.4
        if dist > gap + 0.2:
            tx, tz = ceo.x - dx / dist * gap, ceo.z - dz / dist * gap
            running = dist > 5.0
            speed = locomotion.RUN_SPEED if running else locomotion.WALK_SPEED
            locomotion.move_toward(self.pet, tx, tz, speed, dt)
            self.pet.x, self.pet.z = self.park.collide(self.pet.x, self.pet.z)
            locomotion.apply_anim(self.pet, moving=True, running=running)
        else:
            locomotion.face_dir(self.pet, dx, dz, dt)
            locomotion.apply_anim(self.pet, moving=False)
        self.pet.update(dt, self.registry)

    def _draw_civilian_talk(self) -> None:
        lines = self._civilian_lines()
        is_last = self._civilian_talk == len(lines) - 1
        if is_last and self._pet_stage == 2:
            action = (f"Press  E / X  to accept  +${self.PET_REWARD:,}", True)
        elif is_last:
            action = ("Press  E / X  to accept", True)
        else:
            action = ("Press  E / X  to continue", False)
        self._draw_speech_box("WALTER", "worried pet owner", lines[self._civilian_talk],
                              pr.Color(210, 200, 130, 255), action)

    # -- Río's stolen-guitar quest (find the guitar, return it for a reward) ---
    GUITAR_REWARD = 2500
    BUSKER_PARK_ID = "willow_park"
    GUITAR_PARK_ID = "cedar_grove"
    BUSKER_OFFER_LINES = [
        "Hey… you got a kind face. Some lowlife grabbed my guitar while I was playing.",
        "That guitar's how I eat. I saw him bolt toward Cedar Grove and ditch it in the bushes.",
        "I can't leave my pitch or I'll lose it. Could you go grab it for me?",
        "Bring it back and the $2,500 in my tip jar is yours. Please — it's all I've got.",
    ]
    BUSKER_THANKS_LINES = [
        "No way — you actually got it back! Let me see her… not a scratch!",
        "You don't know what this means. Here, the whole jar — $2,500, every cent. You earned it.",
    ]

    def _busker_park(self):
        parks = self.park.parks
        return (next((g for g in parks if g.id == self.BUSKER_PARK_ID), None)
                or (parks[0] if parks else None))

    def _guitar_park(self):
        parks = self.park.parks
        return (next((g for g in parks if g.id == self.GUITAR_PARK_ID), None)
                or (parks[-1] if parks else None))

    def _busker_lines(self):
        return self.BUSKER_THANKS_LINES if self._guitar_stage == 2 else self.BUSKER_OFFER_LINES

    def _talk_to_busker(self) -> None:
        if self._guitar_done or self._guitar_stage not in (0, 2):
            return
        self._busker_talk = 0
        self._e_cooldown = 8
        voice.speak(self._busker_lines()[0], self._busker_voice)
        while pr.get_char_pressed() > 0:
            pass

    def _update_busker_talk(self) -> None:
        advance = (pr.is_key_pressed(pr.KEY_E) or pr.is_key_pressed(pr.KEY_ENTER)
                   or pr.is_key_pressed(pr.KEY_SPACE) or gamepad.pressed(gamepad.CROSS)
                   or gamepad.pressed(gamepad.TRIANGLE))
        if not advance:
            return
        lines = self._busker_lines()
        self._busker_talk += 1
        if self._busker_talk >= len(lines):
            self._finish_busker()
        else:
            voice.speak(lines[self._busker_talk], self._busker_voice)

    def _finish_busker(self) -> None:
        self._busker_talk = None
        self._e_cooldown = 8
        if self._guitar_stage == 0:               # accepted → go search Cedar Grove
            self._guitar_stage = 1
            where = (self._guitar_park().name if self._guitar_park() else "the park")
            self._toast(f"Find the guitar near {where}.")
            self.inbox.post("Río", f"Thanks for helping. The guitar's stashed near {where} "
                            f"— bring it back and ${self.GUITAR_REWARD:,} is yours.",
                            kind="system", subject="Stolen guitar", ts=pr.get_time())
        elif self._guitar_stage == 2:             # returned → reward
            self._guitar_stage = 3
            self._guitar_done = True
            self.cash += self.GUITAR_REWARD
            self.link.set_flag("guitar_quest", True)
            self._persist_state(force=True)
            self.inbox.post("Río", f"You brought my guitar home. ${self.GUITAR_REWARD:,}, as "
                            "promised — and a song dedicated to you tonight. Thank you.",
                            kind="system",
                            subject=f"✓ Guitar recovered · +${self.GUITAR_REWARD:,}",
                            ts=pr.get_time())

    def _find_guitar(self) -> None:
        """Pick up the stashed guitar; now carry it back to Río."""
        if self._guitar_stage != 1:
            return
        self._guitar_stage = 2
        self._e_cooldown = 8
        self._toast("You found the guitar! Take it back to Río.")

    def _draw_guitar_prop(self) -> None:
        """A simple guitar lying in the park: a body + neck from primitives."""
        gx, gz = self._guitar_pos
        body = pr.Color(150, 95, 45, 255)        # warm wood
        pr.draw_cube(pr.Vector3(gx, 0.35, gz), 0.55, 0.7, 0.18, body)
        pr.draw_cube(pr.Vector3(gx, 0.35, gz + 0.55), 0.12, 0.12, 0.95,
                     pr.Color(80, 55, 30, 255))  # neck
        pr.draw_cube_wires(pr.Vector3(gx, 0.35, gz), 0.55, 0.7, 0.18, pr.Color(40, 28, 14, 255))

    def _draw_busker_talk(self) -> None:
        lines = self._busker_lines()
        is_last = self._busker_talk == len(lines) - 1
        if is_last and self._guitar_stage == 2:
            action = (f"Press  E / X  to accept  +${self.GUITAR_REWARD:,}", True)
        elif is_last:
            action = ("Press  E / X  to accept", True)
        else:
            action = ("Press  E / X  to continue", False)
        self._draw_speech_box("RÍO", "street musician", lines[self._busker_talk],
                              pr.Color(130, 190, 215, 255), action)

    # -- transient park toast (brief on-screen note) --------------------------
    def _toast(self, msg: str) -> None:
        self._park_toast = msg
        self._park_toast_until = pr.get_time() + 3.5

    def _draw_park_toast(self) -> None:
        if not self._park_toast or pr.get_time() > self._park_toast_until:
            return
        tw = pr.measure_text(self._park_toast, 20)
        x = (config.WINDOW_WIDTH - tw) // 2
        y = config.WINDOW_HEIGHT - 150
        pr.draw_rectangle(x - 14, y - 8, tw + 28, 36, pr.Color(0, 0, 0, 175))
        pr.draw_text(self._park_toast, x, y, 20, pr.Color(255, 220, 150, 255))

    # -- Good-Deeds side-quests <-> To-Do board -------------------------------
    # Persistent flag -> To-Do task key, so finished deeds show ✓ in the Nokia.
    _DEED_FLAGS = (("bob_gift", "q_bob"), ("starter_office", "q_starter"),
                   ("pet_quest", "q_pet"), ("guitar_quest", "q_guitar"))

    def _sync_deeds(self) -> None:
        """Reflect the persistent Good-Deed flags onto the To-Do board (display only;
        the flags remain the source of truth)."""
        for flag, key in self._DEED_FLAGS:
            if self.link.load_flag(flag):
                self.taskboard.complete(key)

    def _mark_deed(self, key: str) -> None:
        """Tick a Good-Deed off the To-Do board the moment it's completed."""
        if self.taskboard.complete(key):
            self.link.save_tasks(self.taskboard.done)

    # -- to-do guide (pick a to-do on the Nokia → the city points the way) ----
    # Roles you hire (no quest-stop of their own) all point to the staffing agency.
    _GUIDE_HIRE_TASKS = {"engineer", "designer", "researcher", "marketer", "analyst"}

    def _guide_target_for(self, key):
        """Where in the city this to-do gets done, as (x, z, label) — or None if it
        has no place to walk to (it's done from your desk/phone, or already finished).
        Resolved live so the marker moves with the world (e.g. a lot you lease)."""
        if key is None or self.taskboard.is_done(key):
            return None
        # 1) A quest-stop / workshop building that completes this exact to-do.
        for n in self.park.npc:
            if key in n.task_keys():
                return (n.x, n.z, n.name)
        # 2) Hiring a role → the staffing agency (also reachable from the phone).
        if key in self._GUIDE_HIRE_TASKS:
            for n in self.park.npc:
                if n.store == "hire":
                    return (n.x, n.z, n.name)
        # 3) Your first office → the nearest open lease lot.
        if key == "office":
            lots = [b for b in self.park.buildings
                    if b.status == "available" and not getattr(b, "locked", False)]
            if lots:
                ceo = self.player.ch
                b = min(lots, key=lambda b: math.hypot(b.x - ceo.x, b.z - ceo.z))
                return (b.x, b.z, f"{b.name} (lease)")
        # 4) Your first intern → waiting out in a park.
        if key == "intern" and self._intern_park is not None:
            return (self.intern.x, self.intern.z, self.intern.name)
        # 5) Good-Deeds side-quests → the current step's spot (stage-aware).
        if key == "q_bob" and not self._bob_done:
            return (self.bob.x, self.bob.z, self.bob.name)
        if key == "q_starter" and not self._starter_unlocked:
            return (self.lady.x, self.lady.z, self.lady.name)
        if key == "q_pet" and not self._pet_done:
            if self._pet_stage == 1:                  # go fetch Biscuit
                return (self.pet.x, self.pet.z, self.PET_NAME)
            return (self.civilian.x, self.civilian.z, self.civilian.name)  # talk to Walter
        if key == "q_guitar" and not self._guitar_done:
            if self._guitar_stage == 1:              # go grab the guitar
                return (self._guitar_pos[0], self._guitar_pos[1], "the stolen guitar")
            return (self.busker.x, self.busker.z, self.busker.name)        # talk to Río
        return None

    def _start_guide(self, key):
        """Phone → 'Guide me' on a to-do. Returns (ok, lcd_message). On success the
        park shows a gold beacon + arrow to the spot; the phone closes so you can walk."""
        task = tasks.TASK_BY_KEY.get(key)
        if task is not None and self.taskboard.is_done(key):
            return (False, "Already done ✓")
        tgt = self._guide_target_for(key)
        if tgt is None:
            return (False, "No place to walk to — do this from your office or phone.")
        self._guide_key = key
        title = task.title if task else "your to-do"
        self._toast(f"Guiding you to {tgt[2]} — {title}. Follow the gold marker.")
        return (True, "")

    def _clear_guide(self) -> None:
        self._guide_key = None

    def _draw_guide_hud(self, guide) -> None:
        """On-screen help once a to-do guide is on: a top chip with the target +
        distance, and (when the spot is off-screen) an arrow at the screen edge
        pointing the way. Mirrors the gold world beacon."""
        gx, gz, label = guide
        ceo = self.player.ch
        dist = math.hypot(gx - ceo.x, gz - ceo.z)
        gold = pr.Color(255, 214, 130, 255)
        chip = f"▸ Go to: {label}    {dist:0.0f}m    ·    G to cancel"
        w = pr.measure_text(chip, 18)
        cx = config.WINDOW_WIDTH // 2
        pr.draw_rectangle(cx - w // 2 - 12, 92, w + 24, 28, pr.Color(72, 54, 14, 220))
        pr.draw_text(chip, cx - w // 2, 96, 18, gold)
        # Off-screen arrow: project the target, fall back to a screen-edge pointer.
        cam = self.camera.camera
        sp = pr.get_world_to_screen(pr.Vector3(gx, 2.0, gz), cam)
        fx, fz = cam.target.x - cam.position.x, cam.target.z - cam.position.z
        behind = (gx - cam.position.x) * fx + (gz - cam.position.z) * fz <= 0
        W, H = config.WINDOW_WIDTH, config.WINDOW_HEIGHT
        on = (not behind and 0 <= sp.x <= W and 56 <= sp.y <= H)
        scx, scy = W / 2, H / 2
        dx, dy = sp.x - scx, sp.y - scy
        if behind:
            dx, dy = -dx, -dy
        if on or (dx == 0 and dy == 0):
            return                                 # target is comfortably in view
        ang = math.atan2(dy, dx)
        mx, my = W / 2 - 70, H / 2 - 70            # clamp onto a centred box
        span = min(mx / abs(math.cos(ang)) if math.cos(ang) else 1e9,
                   my / abs(math.sin(ang)) if math.sin(ang) else 1e9)
        ax, ay = scx + math.cos(ang) * span, scy + math.sin(ang) * span
        tip = pr.Vector2(ax + math.cos(ang) * 18, ay + math.sin(ang) * 18)
        lft = pr.Vector2(ax + math.cos(ang + 2.5) * 16, ay + math.sin(ang + 2.5) * 16)
        rgt = pr.Vector2(ax + math.cos(ang - 2.5) * 16, ay + math.sin(ang - 2.5) * 16)
        pr.draw_circle(int(ax), int(ay), 17, pr.Color(72, 54, 14, 220))   # backing so it always reads
        pr.draw_triangle(tip, lft, rgt, gold)         # raylib culls one winding...
        pr.draw_triangle(tip, rgt, lft, gold)         # ...so draw both; only the front face shows

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
            self._buy_seq += 1
            if self.mode == "park":
                # Bought at Bolt Hardware in the city → delivered near your office's
                # entrance (the CEO is out in the park, not standing in the office).
                ent = self.plan.point("entrance") or \
                    self.plan.grid_to_world(self.plan.cols / 2.0, self.plan.rows - 2)
                x = ent[0] + ((self._buy_seq % 5) - 2) * 0.8   # fan out so they don't stack
                z = ent[1]
            else:
                ceo = self.player.ch
                yaw = math.radians(ceo.yaw)
                x = ceo.x + math.sin(yaw) * 1.7    # a step in front of the player
                z = ceo.z + math.cos(yaw) * 1.7
            hx = config.GRID_COLS * config.TILE / 2.0 - 0.8
            hz = config.GRID_ROWS * config.TILE / 2.0 - 0.8
            x, z = max(-hx, min(hx, x)), max(-hz, min(hz, z))
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

    # -- front-desk receptionist: greet the CEO on walk-up --------------------

    def _reception_agent(self) -> Character | None:
        """The reception-role bot on the active floor (its home_room is the lobby,
        so it's only here in a lobby that has one), or None."""
        for a in self.agents:
            if a.brain is not None and self._is_reception(a):
                return a
        return None

    def _reception_line(self, rec: Character) -> str:
        """A short, warm front-desk greeting, personalized with the company name
        when one's been decided."""
        name = (self.company.get("name") or "").strip()
        place = name or "the office"
        first = (rec.name or "").split(" ")[0] or "the front desk"
        return random.choice([
            f"Welcome to {place}! The team's upstairs — need anything?",
            f"Hey boss! Good to see you at {place} — everyone's hard at work up top.",
            f"Morning! {first} here at the front desk; give me a shout if you need anything.",
            "Welcome back! Want me to point you to anyone on the team?",
        ])

    def _greet_at_reception(self) -> None:
        """Once per lobby visit, when the CEO walks within talk range of the
        receptionist, have them greet — a world speech bubble AND aloud."""
        if self._reception_greeted:
            return
        if self.chat.open or self.phone.open or self.meeting.open or self.drive.open:
            return                              # don't talk over an open panel
        rec = self._reception_agent()
        if rec is None:
            return
        ceo = self.player.ch
        if math.hypot(rec.x - ceo.x, rec.z - ceo.z) > TALK_RANGE:
            return
        self._reception_greeted = True
        line = self._reception_line(rec)
        rec.brain.say(line, secs=4.5)                   # world speech bubble
        voice.speak(line, voice.pick_voice(rec.name))   # ...and aloud (macOS say)

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

    # -- co-founder follow ("Robin, walk with me") ----------------------------
    def _set_robin_following(self, on: bool) -> None:
        """Phone-driven toggle for Robin trailing the CEO. Switching it on drops
        him in beside you wherever you're standing (office or park) so he never has
        to sprint across the map to catch up. He acks the change aloud (in his own
        voice), but only on a real state change so re-tapping the same row is quiet."""
        on = bool(on)
        changed = on != self.robin_following
        self.robin_following = on
        if on:
            self._snap_robin_to_ceo()
        if changed:
            line = "On my way — right behind you." if on else "Got it, I'll wait here."
            voice.speak(line, self._robin_voice)

    def _snap_robin_to_ceo(self) -> None:
        """Place Robin a step behind/beside the CEO. Used when follow turns on and
        on every mode/room switch, so he arrives with you instead of teleporting in
        from his last position in the other space."""
        ceo = self.player.ch
        self.robin.x, self.robin.y, self.robin.z = ceo.x + 1.2, 0.0, ceo.z - 1.2
        self.robin.yaw = ceo.yaw

    def _update_companion(self, dt: float) -> None:
        """Trail the CEO when follow is on — identical steering in the office and the
        park. Uses direct move_toward (no navgrid, which the park doesn't have): the
        CEO already threads the obstacles, and Robin keeps a gap behind, running to
        close a big lead and idling (facing you) once he's caught up."""
        if not self.robin_following:
            return
        ceo, robin = self.player.ch, self.robin
        dx, dz = ceo.x - robin.x, ceo.z - robin.z
        dist = math.hypot(dx, dz)
        GAP = 1.8
        if dist > GAP + 0.2:
            tx, tz = ceo.x - dx / dist * GAP, ceo.z - dz / dist * GAP
            running = dist > 6.0
            speed = locomotion.RUN_SPEED if running else locomotion.WALK_SPEED
            locomotion.move_toward(robin, tx, tz, speed, dt)
            if self.mode == "park":
                robin.x, robin.z = self.park.collide(robin.x, robin.z)
            locomotion.apply_anim(robin, moving=True, running=running)
        else:
            locomotion.face_dir(robin, dx, dz, dt)
            locomotion.apply_anim(robin, moving=False)
        robin.update(dt, self.registry)

    # -- phone city map (data; the phone draws it) ----------------------------
    def _map_here(self):
        """The 'you are here' pin: your live park position (with facing) when you're
        outside, or the building you're standing in when you're indoors (office
        coords don't map onto the city, so we pin the building instead)."""
        if self.mode == "park":
            p = self.player.ch
            return (p.x, p.z, p.yaw, True)        # True = draw a facing arrow
        b = self.current_building
        if b is not None:
            return (b.x, b.z, 0.0, False)         # inside this building
        return None

    def _map_markers(self) -> list[dict]:
        """Every city POI for the phone map: your offices, lease lots, shops, quest
        stops, storefronts, the bank/broker, parks — plus Robin and the intern when
        they're out in the world. Each is world (x,z) + colour + label + kind."""
        out: list[dict] = []
        for b in self.park.buildings:
            leased = b.status != "available"
            # Your own buildings all share one bright "yours" gold (matches the map
            # legend) so they read as a group; lease lots are orange.
            if b.status == "hq":
                col, kind, label = (255, 222, 120), "hq", b.name
            elif leased:
                col, kind, label = (255, 222, 120), "office", b.name
            elif getattr(b, "locked", False):    # gated lot (e.g. Starter Office)
                col, kind, label = (150, 155, 165), "locked", f"{b.name} (locked)"
            else:
                col, kind, label = (235, 150, 70), "lease", f"{b.name} — lease ${b.deposit:,}"
            out.append({"x": b.x, "z": b.z, "color": col, "label": label, "kind": kind})
        for n in self.park.npc:
            if n.market:
                col, kind = (240, 200, 90), "bank"
            elif n.store:
                col, kind = (190, 130, 230), "store"
            elif n.task or n.tasks:
                col, kind = (90, 210, 230), "quest"
            else:
                col, kind = (200, 205, 215), "shop"
            out.append({"x": n.x, "z": n.z, "color": col, "label": n.name, "kind": kind})
        for g in self.park.parks:
            out.append({"x": g.x, "z": g.z, "color": (120, 200, 120),
                        "label": g.name, "kind": "park"})
        if self.robin_following:
            out.append({"x": self.robin.x, "z": self.robin.z, "color": (90, 230, 245),
                        "label": f"{COFOUNDER_NAME} (with you)", "kind": "robin"})
        if self._intern_park is not None and not self.taskboard.is_done("intern"):
            out.append({"x": self.intern.x, "z": self.intern.z, "color": (150, 230, 150),
                        "label": "Eager Intern", "kind": "intern"})
        if not self._bob_done:
            out.append({"x": self.bob.x, "z": self.bob.z, "color": (225, 170, 120),
                        "label": "Bob (old friend)", "kind": "friend"})
        elif self._bob_rescue_pending and not self._bob_rescue_done:
            out.append({"x": self.bob.x, "z": self.bob.z, "color": (225, 170, 120),
                        "label": "Bob (meet up)", "kind": "friend"})
        if not self._starter_unlocked:
            out.append({"x": self.lady.x, "z": self.lady.z, "color": (220, 150, 195),
                        "label": "Mae (Starter Office)", "kind": "friend"})
        if not self._pet_done:
            if self._pet_stage in (0, 2):        # Walter wants to talk (offer / collect)
                out.append({"x": self.civilian.x, "z": self.civilian.z, "color": (220, 210, 130),
                            "label": "Walter (lost pet)", "kind": "friend"})
            if self._pet_stage == 1:             # Biscuit is out there to be found
                out.append({"x": self.pet.x, "z": self.pet.z, "color": (220, 180, 140),
                            "label": f"{self.PET_NAME} (lost dog)", "kind": "friend"})
        if not self._guitar_done:
            if self._guitar_stage in (0, 2):     # Río wants to talk (offer / collect)
                out.append({"x": self.busker.x, "z": self.busker.z, "color": (130, 190, 215),
                            "label": "Río (stolen guitar)", "kind": "friend"})
            if self._guitar_stage == 1:          # the guitar is out there to be found
                out.append({"x": self._guitar_pos[0], "z": self._guitar_pos[1],
                            "color": (180, 120, 60), "label": "Stolen guitar", "kind": "friend"})
        return out

    # -- save game (cash + leases) --------------------------------------------
    def _persist_state(self, force: bool = False) -> None:
        """Write cash + leased offices through to the store when they change. Cash
        moves in dozens of places (rent, hiring, shopping, the market, rewards), so
        rather than save at every call site we snapshot here once per frame, throttled
        to ~2s of real time, plus a forced flush on quit / New World."""
        now = pr.get_time()
        if not force and now - self._last_persist < 2.0:
            return
        self._last_persist = now
        cash = int(self.cash)
        if force or cash != self._saved_cash:
            self.link.save_cash(cash)
            self._saved_cash = cash
        if force or self.calendar.day != self._saved_day:
            self.link.save_calendar(self.calendar.to_state())
            self._saved_day = self.calendar.day
        leases = {b.id for b in self.park.buildings if b.status == "leased"}
        if force or leases != self._saved_leases:
            self.link.save_leases(leases)
            self._saved_leases = leases

    # -- office park ----------------------------------------------------------
    def _enter_park(self) -> None:
        # Leaving a quest building: restore your real office and drop the visit state.
        if self._quest_building is not None:
            if self._saved_office is not None:
                self.current_building, self.interior = self._saved_office
            self._quest_building = self._quest_actor = self._saved_office = None
            self._close_quest_input()
            self.scene.show_records = True
        self.mode = "park"
        self.selected = -1
        self._e_cooldown = 2       # don't let an exiting E press re-enter a building
        locomotion.set_bounds(*self.park.bounds)
        ceo = self.player.ch
        # Drop back where you entered a building, else the default spawn.
        if self._park_return is not None:
            ceo.x, ceo.z = self._park_return
            self._park_return = None
        else:
            ceo.x, ceo.z = self.park.spawn
        ceo.y, ceo.yaw = 0.0, 0.0       # face downtown / HQ (toward +z)
        if self.robin_following:         # bring your co-founder out into the city too
            self._snap_robin_to_ceo()
        # Point the camera the same way (behind the CEO, looking at HQ), else it
        # spawns facing away and HQ is off-screen behind you.
        self.camera.yaw = math.radians(180.0)
        self.camera.pitch = math.radians(20.0)
        self.camera.distance = 10.0
        # Stand your first intern out on a park lawn (Founders Green), off the
        # fountain/path, until you take them on.
        self._intern_park = next((g for g in self.park.parks if g.id == "founders_green"),
                                 self.park.parks[0] if self.park.parks else None)
        if self._intern_park is not None:
            self.intern.x = self._intern_park.x + 2.2
            self.intern.z = self._intern_park.z + 1.2
            self.intern.y = 0.0
        # Until you've taken his gift, Bob stands a few steps ahead of the spawn —
        # the first face you see arriving in the city — turned to greet you.
        if not self._bob_done:
            sx, sz = self.park.spawn
            self.bob.x, self.bob.z, self.bob.y = sx + 1.2, sz + 2.6, 0.0
            self.bob.yaw = 180.0
        elif self._bob_rescue_pending and not self._bob_rescue_done:
            spot = self._bob_rescue_spot()       # he's waiting at the park to bail you out
            if spot is not None:
                self.bob.x, self.bob.z, self.bob.y = spot.x + 2.0, spot.z + 1.0, 0.0
        # Mae waits in her park until you've unlocked the Starter Office.
        if not self._starter_unlocked:
            lp = self._lady_park()
            if lp is not None:
                self.lady.x, self.lady.z, self.lady.y = lp.x - 2.0, lp.z + 1.0, 0.0
                self.lady.yaw = 180.0
        # Walter waits in his park; Biscuit waits in his until found (then he follows).
        if not self._pet_done:
            cp = self._civilian_park()
            if cp is not None:
                self.civilian.x, self.civilian.z, self.civilian.y = cp.x - 2.0, cp.z + 1.0, 0.0
                self.civilian.yaw = 180.0
            if self._pet_stage < 2:
                pp = self._pet_park()
                if pp is not None:
                    self.pet.x, self.pet.z, self.pet.y = pp.x + 1.5, pp.z - 1.0, 0.0
        # Río waits in her park; the stolen guitar is stashed at another park.
        if not self._guitar_done:
            bp = self._busker_park()
            if bp is not None:
                self.busker.x, self.busker.z, self.busker.y = bp.x + 2.0, bp.z + 1.0, 0.0
                self.busker.yaw = 180.0
            gp = self._guitar_park()
            if gp is not None:
                self._guitar_pos = (gp.x - 1.5, gp.z + 1.5)

    def _enter_office(self, building=None) -> None:
        # Always arrive in the building's lobby (then ride up to the wings). A new
        # building builds its interior first.
        if self.mode == "park":            # remember where to drop you back on exit
            self._park_return = (self.player.ch.x, self.player.ch.z)
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

    # -- company records cabinet (opens the Dossier) --------------------------
    RECORDS_REACH = 2.2

    def _near_records(self) -> bool:
        """True when the CEO is standing by the office records cabinet."""
        ceo = self.player.ch
        rx, rz = self.scene.records_pos()
        return math.hypot(rx - ceo.x, rz - ceo.z) < self.RECORDS_REACH

    def _draw_records_prompt(self) -> None:
        text = "Press  E  to open the Company Files   (or  C  anywhere)"
        tw = pr.measure_text(text, 20)
        x = (config.WINDOW_WIDTH - tw) // 2
        pr.draw_rectangle(x - 12, config.WINDOW_HEIGHT - 132, tw + 24, 32, pr.Color(0, 0, 0, 160))
        pr.draw_text(text, x, config.WINDOW_HEIGHT - 126, 20, pr.RAYWHITE)

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
        self._reception_greeted = False    # re-arm the front-desk hello for this room
        self._show_room(room_key)          # seat this room's agents (mutates live lists)
        self._rebuild_nav()
        for a in self.agents:              # deskless overflow: park by the meeting table
            if a.desk is None:
                a.seat = None
                a.x, a.z = self.plan.primary_meeting()
        ceo = self.player.ch
        ceo.x, ceo.z = entry if entry is not None else \
            self.plan.grid_to_world(self.plan.cols / 2 - 0.5, self.plan.rows - 2)
        if self.robin_following:           # co-founder steps into the new room with you
            self._snap_robin_to_ceo()

    # -- quest buildings: walk in, talk to the NPC inside ---------------------
    QUEST_PLAN = "hq"          # default interior floor used for every quest building

    def _enter_quest_building(self, npc) -> None:
        """Walk into a quest building like an office: load a default floor with one
        NPC to talk to (reuses the interior system). Your real office is saved and
        restored when you leave. Talk to the NPC (E) to do the quest."""
        if self.mode == "park":            # remember where to drop you back on exit
            self._park_return = (self.player.ch.x, self.player.ch.z)
        self._saved_office = (self.current_building, self.interior)
        self._quest_building = npc
        self.interior = interior.build_interior(
            npc.id, None, getattr(npc, "plan", None) or self.QUEST_PLAN, self.plans)
        self.current_building = None
        self.mode = "office"
        self.scene.show_records = False        # the Files cabinet is your office only
        lobby = self.interior.entry_room
        plan = self.interior.rooms[lobby].plan(self.plans)
        ent = plan.point("door") or plan.grid_to_world(plan.cols / 2.0, plan.rows - 1.5)
        self._activate_room(lobby, entry=ent)
        self._spawn_quest_actor(npc)
        ceo = self.player.ch
        ceo.y = 0.0
        self._office_spawn = (ceo.x, ceo.z)
        self._e_cooldown = 3                   # don't let the entering E talk immediately

    _STORE_ACTOR = {"outfit": "Tailor", "hire": "Recruiter"}
    _SERVICE_ACTOR = {"grant": "Grants Officer"}
    _GAME_ACTOR = {"slots": "Pit Boss", "farm": "Trade Attaché"}

    def _spawn_quest_actor(self, npc) -> None:
        """Place one NPC inside the building to talk to — Robin for the cafe, a
        shopkeeper for a store, a clerk for a service, else named after the beat."""
        keys = npc.pending(self.taskboard.done) or list(npc.task_keys())
        key = keys[0] if keys else ""
        lines = dialogue.lines_for(self.dialogue, key)
        name = ((self._STORE_ACTOR.get(npc.store) if npc.is_store else None)
                or (self._SERVICE_ACTOR.get(npc.service) if npc.is_service else None)
                or (self._GAME_ACTOR.get(npc.game) if npc.is_game else None)
                or (lines[0].who if lines else "")) or npc.name
        cx, cz = self.plan.grid_to_world(self.plan.cols / 2.0 - 0.5, 2.0)
        if npc.task == "cofounder":            # the cafe: it's Robin in person
            actor = self.robin
            actor.name = name or COFOUNDER_NAME
        else:
            actor = Character(name=name, role="", x=cx, z=cz,
                              color=pr.Color(120, 200, 160, 255), dept="",
                              model="Suit_Male.gltf", yaw=0.0)
            # Name-seeded look so each quest NPC has a real (non-black) skin tone,
            # distinct but stable across runs.
            roster.apply_look(actor, roster.random_look(random.Random(name)))
        actor.x, actor.z, actor.y = cx, cz, 0.0
        ceo = self.player.ch
        actor.yaw = math.degrees(math.atan2(ceo.x - actor.x, ceo.z - actor.z))
        self._quest_actor = actor
        if actor not in self.characters:
            self.characters.append(actor)

    def _near_quest_actor(self) -> bool:
        a = self._quest_actor
        if a is None:
            return False
        ceo = self.player.ch
        return math.hypot(a.x - ceo.x, a.z - ceo.z) < TALK_RANGE

    def _draw_quest_actor_prompt(self) -> None:
        b = self._quest_building
        name = self._quest_actor.name if self._quest_actor else "them"
        if b is not None and b.store == "outfit":
            text = "Press  E  to change your outfit"
        elif b is not None and b.store == "hire":
            text = "Press  E  to hire agents"
        elif b is not None and b.service == "grant":
            text = "Press  E  to apply for a grant"
        else:
            text = f"Press  E  to talk to {name}"
        tw = pr.measure_text(text, 20)
        x = (config.WINDOW_WIDTH - tw) // 2
        pr.draw_rectangle(x - 12, config.WINDOW_HEIGHT - 132, tw + 24, 32, pr.Color(0, 0, 0, 160))
        pr.draw_text(text, x, config.WINDOW_HEIGHT - 126, 20, pr.RAYWHITE)

    def _draw_quest_building_hud(self) -> None:
        """Minimal HUD inside a quest building: name bar + talk/leave prompts (no
        office buttons). While a conversation is open, the dialogue modal owns it."""
        npc = self._quest_building
        pr.draw_rectangle(0, 0, config.WINDOW_WIDTH, 56, pr.Color(20, 24, 34, 230))
        pr.draw_text(npc.name, 18, 14, 28, pr.RAYWHITE)
        cash = f"Cash: ${self.cash:,}"
        pr.draw_text(cash, config.WINDOW_WIDTH - pr.measure_text(cash, 22) - 18, 18, 22, pr.GOLD)
        if self.investor.open:                   # Apex Ventures: the pitch panel
            self._do_investor_action(self.investor.draw(self.company, self._rounds_raised()))
            return
        if self.market_panel.open:               # bank/broker: the trading terminal
            self._do_market_action(self.market_panel.draw(self.market, self.cash))
            return
        if self.slot_panel.open:                 # the casino: the slot machine
            self._do_slot_action(self.slot_panel.draw(self.cash))
            return
        if self.farm_panel.open:                 # the Trade Embassy: the idle farm
            self._do_farm_action(self.farm_panel.draw(self.farm, self.cash))
            return
        if self.grant_panel.open:                # Grants Office: LLM-judged funding
            self._drive_grant_panel()
            return
        if self._quest_input is not None:        # mid-conversation: the modal handles it
            self._draw_quest_input()
            return
        if self._near_quest_actor():
            self._draw_quest_actor_prompt()
        else:
            portal = self._nearest_portal()
            if portal is not None:
                self._draw_portal_prompt(portal)

    def _drive_grant_panel(self) -> None:
        """Draw the Grants Office panel and drive its async LLM review: submit kicks
        off the off-thread judge; while reviewing we poll; a verdict pays any award
        and posts the result. The board's decision is the LLM's, not scripted."""
        action = self.grant_panel.draw()
        if action and action[0] == "submit":
            if self.link.request_grant(action[1], self.company):
                self.grant_panel.set_reviewing()
        elif self.grant_panel.state == "reviewing":
            verdict = self.link.poll_grant()
            if verdict is not None:
                if verdict.get("approved") and verdict.get("amount", 0) > 0:
                    self.cash += int(verdict["amount"])
                    self.inbox.post(
                        "Grants Office",
                        f"Approved — {verdict.get('program', 'grant')} for "
                        f"${int(verdict['amount']):,}. {verdict.get('feedback', '')}",
                        kind="system", subject="✓ Grant approved", ts=pr.get_time())
                else:
                    self.inbox.post(
                        "Grants Office",
                        f"Application declined. {verdict.get('feedback', '')}",
                        kind="system", subject="Grant declined", ts=pr.get_time())
                self.grant_panel.set_result(verdict)

    def _visit_quest_stop(self, npc) -> None:
        """Talk to a quest building's NPC. Picks its next unfinished to-do (a
        workshop like the Incubator steps through several); if that to-do asks for a
        decision, open a text field to capture it — so the answer reaches the agents'
        brains. Otherwise complete it outright."""
        if npc.task == "raise_round":          # the VC firm: an investor meeting, not a to-do
            self.investor.open_panel()
            self._e_cooldown = 8
            return
        if getattr(npc, "market", None):       # the bank / broker: the idle trading terminal
            pending = npc.pending(self.taskboard.done)
            if pending:                        # do its one-time quest first (the bank's pricing)
                self._open_quest_task(npc, pending[0])
                return
            self.market_panel.open_panel(npc.market)
            self._e_cooldown = 8
            return
        if getattr(npc, "service", None) == "grant":   # the Grants Office: LLM-judged funding
            self.grant_panel.open_panel()
            self._e_cooldown = 8
            return
        g = getattr(npc, "game", None)             # arcade/idle venue (casino slots, embassy farm)
        if g in ("slots", "farm"):
            pending = npc.pending(self.taskboard.done)
            if pending:                            # do any one-time intro quest first
                self._open_quest_task(npc, pending[0])
                return
            (self.slot_panel if g == "slots" else self.farm_panel).open_panel()
            self._e_cooldown = 8
            return
        pending = npc.pending(self.taskboard.done)
        if not pending:
            # A finished stop still talks back on later visits — replay the NPC's
            # lines on screen (visible feedback), with no second reward (the
            # complete() at the end no-ops once it's already done). This covers the
            # Angel Investor, the civic clerks, the city guide, etc.
            key = npc.task
            if key and key in self.dialogue:
                self._open_lore(npc, key, replay=True)
                return
            self.inbox.post(npc.name, "Already taken care of — nothing else to do here.",
                            kind="system", subject=npc.name, ts=pr.get_time())
            self._e_cooldown = 8
            return
        self._open_quest_task(npc, pending[0])

    def _open_quest_task(self, npc, key: str) -> None:
        """Begin one of a quest stop's to-dos: capture text if it asks, play a
        dialogue-only beat (then complete) if it just has lines, else finish it."""
        task = tasks.TASK_BY_KEY.get(key)
        if task is not None and task.ask and task.field:
            self._quest_input, self._quest_task, self._quest_buf = npc, key, ""
            self._quest_line, self._quest_action = 0, None  # start at the beat's first line
            self._e_cooldown = 4                           # don't let the opening E skip line 1
            while pr.get_char_pressed() > 0:               # drop the 'e' that opened it
                pass
        elif key in self.dialogue:        # lore-only stop (e.g. the city guide): play, then complete
            self._open_lore(npc, key)
        else:
            self._complete_quest_stop(npc, key)

    def _open_lore(self, npc, key: str, replay: bool = False) -> None:
        """Open the dialogue modal in play-then-finish mode (no input field): step
        the beat's lines, then _complete_quest_stop on the last one. The complete()
        is a no-op reward-wise once the to-do is done, so this doubles as the on-
        screen replay when you revisit a finished stop (Angel Investor, clerks…).
        `replay` picks the beat's `done` lines (a fresh line for a finished stop)."""
        self._quest_input, self._quest_task, self._quest_buf = npc, key, ""
        self._quest_line, self._quest_action = 0, "complete"
        self._quest_replay = replay
        self._e_cooldown = 4
        while pr.get_char_pressed() > 0:
            pass

    def _open_store_greeting(self, npc) -> None:
        """A shop NPC (Tailor / Recruiter) greets you, then opening the shop happens
        on the last line — same dialogue modal, but it ends in an action (the editor
        or the Hire app) instead of a typed answer. Beat key = the store kind."""
        self._quest_input, self._quest_task, self._quest_buf = npc, npc.store, ""
        self._quest_line, self._quest_action = 0, npc.store
        self._e_cooldown = 4
        while pr.get_char_pressed() > 0:                   # drop the 'e' that opened it
            pass

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

    def _close_quest_input(self) -> None:
        self._quest_input, self._quest_task, self._quest_buf = None, None, ""
        self._quest_line, self._quest_action = 0, None
        self._quest_replay = False

    def _draw_quest_input(self) -> None:
        """Modal conversation for a quest stop: step through the beat's dialogue
        lines (E/Space to continue), THEN — as a separate step — fill in its `ask`
        and Enter to save. The input field is its own step so the E you press to
        advance never leaks into the text box. Lines from assets/dialogue.json."""
        npc = self._quest_input
        key = self._quest_task
        task = tasks.TASK_BY_KEY.get(key)          # None for a store-greeting beat
        lines = dialogue.lines_for(self.dialogue, key, done=self._quest_replay)
        n = len(lines)
        # _quest_line is 0..n: 0..n-1 are the spoken lines; n is the answer step.
        self._quest_line = max(0, min(self._quest_line, n))
        in_input = self._quest_line >= n
        cur_line = lines[min(self._quest_line, n - 1)]   # answer step keeps the last line up

        sw, sh = pr.get_screen_width(), pr.get_screen_height()
        w, h = 600, 260
        x, y = (sw - w) // 2, (sh - h) // 2
        pr.draw_rectangle(0, 0, sw, sh, pr.Color(0, 0, 0, 160))
        pr.draw_rectangle(x, y, w, h, pr.Color(26, 30, 42, 255))
        pr.draw_rectangle_lines_ex(pr.Rectangle(x, y, w, h), 2, pr.Color(90, 210, 230, 255))
        # speaker tab — the line's own `who`, falling back to the building's name.
        speaker = cur_line.who or npc.name
        pr.draw_rectangle(x, y - 32, max(200, pr.measure_text(speaker, 20) + 28), 32,
                          pr.Color(90, 210, 230, 255))
        pr.draw_text(speaker, x + 16, y - 26, 20, pr.Color(12, 24, 30, 255))
        # Workshop progress (e.g. the canvas): how many blocks remain.
        keys = npc.task_keys()
        if len(keys) > 1:
            step = sum(1 for k in keys if self.taskboard.is_done(k)) + 1
            prog = f"{step}/{len(keys)}"
            pr.draw_text(prog, x + w - pr.measure_text(prog, 18) - 16, y - 26, 18,
                         pr.Color(12, 24, 30, 255))
        # the current spoken line (word-wrapped)
        ly = y + 18
        cur = ""
        for word in cur_line.text.split(" "):
            trial = (cur + " " + word).strip()
            if pr.measure_text(trial, 20) > w - 40 and cur:
                pr.draw_text(cur, x + 20, ly, 20, pr.RAYWHITE)
                cur, ly = word, ly + 26
            else:
                cur = trial
        if cur:
            pr.draw_text(cur, x + 20, ly, 20, pr.RAYWHITE)

        if pr.is_key_pressed(pr.KEY_ESCAPE):
            self._close_quest_input()
            return

        if not in_input:
            # Spoken line: wait for the player to read, then advance to the next line
            # (or to the answer step). Drain the keypress so it can't type into the field.
            is_last = self._quest_line == n - 1
            dots = "." * (1 + int(pr.get_time() * 2) % 3)
            if not is_last:
                nextlabel = "to continue"
            elif self._quest_action == "outfit":
                nextlabel = "to change your outfit"
            elif self._quest_action == "hire":
                nextlabel = "to hire"
            elif self._quest_action == "complete":
                nextlabel = "to wrap up"
            else:
                nextlabel = "to answer"
            pr.draw_text(f"Press  E  {nextlabel}{dots}", x + 20, y + h - 30, 16,
                         pr.Color(150, 200, 230, 255))
            advance = (pr.is_key_pressed(pr.KEY_E) or pr.is_key_pressed(pr.KEY_SPACE)
                       or pr.is_key_pressed(pr.KEY_ENTER)
                       or pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT)
                       or gamepad.pressed(gamepad.CROSS) or gamepad.pressed(gamepad.TRIANGLE))
            if advance and self._e_cooldown == 0:
                if is_last and self._quest_action is not None:   # store: open shop; lore: finish
                    action = self._quest_action
                    self._close_quest_input()
                    if action == "outfit":
                        self._open_outfitter()
                    elif action == "hire":
                        self._open_hire_store()
                    elif action == "complete":           # lore-only stop → tick the to-do + reward
                        self._complete_quest_stop(npc, key)
                    return
                self._quest_line += 1
                self._e_cooldown = 3            # debounce so one press = one step
                while pr.get_char_pressed() > 0:  # don't let this key land in the field
                    pass
            return

        if task is None:                       # safety: a non-ask beat fell through
            self._close_quest_input()
            return
        # Answer step: the input field for this task's `ask`.
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
        if pr.is_key_pressed(pr.KEY_ENTER) and self._quest_buf.strip():
            self._complete_quest_stop(npc, key, self._quest_buf.strip())
            # Workshop flow: roll on to the next unfilled block (restart its dialogue);
            # otherwise close.
            nxt = next((k for k in npc.pending(self.taskboard.done)
                        if tasks.TASK_BY_KEY.get(k) and tasks.TASK_BY_KEY[k].ask), None)
            if nxt is not None:
                self._quest_task, self._quest_buf, self._quest_line = nxt, "", 0
            else:
                self._close_quest_input()

    def _park_frame(self, dt: float) -> None:
        """One frame of the walkable park: move, lease/enter, draw."""
        self.park.update(dt)          # advance ambient city traffic
        ceo = self.player.ch
        # Freeze the world while a quest text field, the dossier, the phone, or
        # an investor meeting is open.
        frozen = (self._quest_input is not None
                  or self.dossier.open or self.investor.open or self.phone.open
                  or self.shop.open or self._bob_talk is not None
                  or self._lady_talk is not None or self._civilian_talk is not None
                  or self._busker_talk is not None)
        # Live to-do guide: re-resolve the chosen to-do's spot each frame so the gold
        # beacon tracks the world; clear it (with a cheer) the moment it's completed.
        guide = self._guide_target_for(self._guide_key) if self._guide_key else None
        if self._guide_key and guide is None:
            if self.taskboard.is_done(self._guide_key):
                self._toast("To-do complete ✓  Nice work.")
            self._guide_key = None
        if self.phone.open:                    # the Nokia works out in the city too
            self.phone.update()
        if self._bob_talk is not None:         # mid-greeting: the speech box owns input
            self._update_bob_talk()
        if self._lady_talk is not None:        # mid-conversation with Mae
            self._update_lady_talk()
        if self._civilian_talk is not None:    # mid-conversation with Walter
            self._update_civilian_talk()
        if self._busker_talk is not None:      # mid-conversation with Río
            self._update_busker_talk()
        # Your first intern waits in a park until taken on (the "intern" quest).
        show_intern = (self._intern_park is not None
                       and not self.taskboard.is_done("intern"))
        # Bob is out here for the welcome gift, or waiting at a park to bail you out.
        show_bob = (not self._bob_done
                    or (self._bob_rescue_pending and not self._bob_rescue_done))
        if not frozen:
            self.player.update(dt, self.camera)
            ceo.x, ceo.z = self.park.collide(ceo.x, ceo.z)   # block walking through buildings
            self.camera.update(dt, self.player.ch)
            ceo.update(dt, self.registry)
            if show_intern:                      # keep the intern turned toward you, idling
                self.intern.yaw = math.degrees(math.atan2(ceo.x - self.intern.x,
                                                          ceo.z - self.intern.z))
                self.intern.update(dt, self.registry)
            self._update_companion(dt)           # Robin trails you across the park
        if show_bob:                             # keep Bob turned toward you, idling
            self.bob.yaw = math.degrees(math.atan2(ceo.x - self.bob.x, ceo.z - self.bob.z))
            self.bob.update(dt, self.registry)
        show_lady = not self._starter_unlocked   # Mae, until the Starter Office is unlocked
        if show_lady:
            self.lady.yaw = math.degrees(math.atan2(ceo.x - self.lady.x, ceo.z - self.lady.z))
            self.lady.update(dt, self.registry)
        # Walter (until his pet is home) and Biscuit (once the search is on).
        show_civilian = not self._pet_done
        show_pet = (not self._pet_done) and self._pet_stage in (1, 2)
        if show_civilian:
            self.civilian.yaw = math.degrees(math.atan2(ceo.x - self.civilian.x,
                                                        ceo.z - self.civilian.z))
            self.civilian.update(dt, self.registry)
        if show_pet:
            if self._pet_stage == 2 and not frozen:
                self._update_pet_follow(dt)      # Biscuit trots home behind you
            else:
                self.pet.yaw = math.degrees(math.atan2(ceo.x - self.pet.x, ceo.z - self.pet.z))
                self.pet.update(dt, self.registry)
        # Río (until her guitar is back) and the stashed guitar (a drawn prop).
        show_busker = not self._guitar_done
        show_guitar = (not self._guitar_done) and self._guitar_stage == 1
        if show_busker:
            self.busker.yaw = math.degrees(math.atan2(ceo.x - self.busker.x,
                                                      ceo.z - self.busker.z))
            self.busker.update(dt, self.registry)
        self.pedestrians.update(dt, ceo.x, ceo.z, self.registry)  # ambient crowd
        near = self.park.nearest(ceo.x, ceo.z)
        # Quest-stop NPC buildings (Chamber of Commerce, …) are only offered when no
        # lease lot is in reach, so E is never ambiguous (they never share a corner).
        near_npc = self.park.nearest_npc(ceo.x, ceo.z) if near is None else None
        intern_near = (show_intern and near is None and near_npc is None
                       and math.hypot(self.intern.x - ceo.x, self.intern.z - ceo.z)
                       <= parkmod.REACH)
        bob_near = (show_bob and near is None and near_npc is None
                    and math.hypot(self.bob.x - ceo.x, self.bob.z - ceo.z) <= parkmod.REACH)
        lady_near = (show_lady and near is None and near_npc is None
                     and math.hypot(self.lady.x - ceo.x, self.lady.z - ceo.z) <= parkmod.REACH)
        # Walter offers/collects at stages 0 and 2; Biscuit is grabbable at stage 1.
        civilian_near = (show_civilian and near is None and near_npc is None
                         and self._pet_stage in (0, 2)
                         and math.hypot(self.civilian.x - ceo.x, self.civilian.z - ceo.z) <= parkmod.REACH)
        pet_near = (show_pet and self._pet_stage == 1 and near is None and near_npc is None
                    and math.hypot(self.pet.x - ceo.x, self.pet.z - ceo.z) <= parkmod.REACH)
        # Río offers/collects at stages 0 and 2; the guitar is grabbable at stage 1.
        busker_near = (show_busker and near is None and near_npc is None
                       and self._guitar_stage in (0, 2)
                       and math.hypot(self.busker.x - ceo.x, self.busker.z - ceo.z) <= parkmod.REACH)
        guitar_near = (show_guitar and near is None and near_npc is None
                       and math.hypot(self._guitar_pos[0] - ceo.x, self._guitar_pos[1] - ceo.z) <= parkmod.REACH)

        if not frozen:
            if pr.is_key_pressed(pr.KEY_P):
                self._enter_office(); return
            if guide is not None and pr.is_key_pressed(pr.KEY_G):   # call off the guide
                self._clear_guide()
                self._toast("Guide off.")
                guide = None
            press_e = self._e_cooldown == 0 \
                and (pr.is_key_pressed(pr.KEY_E) or gamepad.pressed(gamepad.TRIANGLE))
            if near is not None and press_e:
                if near.leased:
                    self._enter_office(near); return
                elif getattr(near, "locked", False):    # gated lot — meet the NPC first
                    self._toast(f"{near.name} is locked — find Mae in a park to open it.")
                    self._e_cooldown = 8
                elif self.cash >= near.deposit:
                    self.cash -= near.deposit
                    self.park.lease(near)        # capacity rises via max_desks
            elif near_npc is not None and press_e:
                if near_npc.store == "shop":
                    # The furniture shop applies to your OFFICE (the active scene in
                    # park mode), so open it right here instead of entering — entering
                    # would repurpose the scene and the decor would land in the store.
                    self.shop.open_()
                    self._e_cooldown = 8
                    return
                # Every other interactive building — quest stop OR storefront — is
                # entered; inside, talk to the NPC (E) to do the quest / open the shop.
                self._enter_quest_building(near_npc); return
            elif intern_near and press_e:        # the park intern → take them on (free)
                self._take_on_intern()
            elif bob_near and press_e:           # childhood friend → welcome gift or rescue
                self._talk_to_bob("welcome" if not self._bob_done else "rescue")
            elif lady_near and press_e:          # Mae → unlock the affordable Starter Office
                self._talk_to_lady()
            elif civilian_near and press_e:      # Walter → offer / collect the lost-pet quest
                self._talk_to_civilian()
            elif pet_near and press_e:           # found Biscuit → he follows you home
                self._find_pet()
            elif busker_near and press_e:        # Río → offer / collect the stolen-guitar quest
                self._talk_to_busker()
            elif guitar_near and press_e:        # found the guitar → carry it back
                self._find_guitar()

        pr.begin_drawing()
        pr.clear_background(self.daylight.sky_color())
        self.park.draw(self.camera.camera, self.season.name, self.taskboard.done)
        # Sit every park character on the gentle terrain — Y only; their X/Z movement,
        # collision and the traffic sim are unchanged, so navigation stays intact.
        gy = self.park.ground_y
        ceo.y = gy(ceo.x, ceo.z)
        for _ch in (getattr(self, _n, None) for _n in
                    ("intern", "bob", "lady", "civilian", "pet", "busker", "robin")):
            if _ch is not None:
                _ch.y = gy(_ch.x, _ch.z)
        for _ped in self.pedestrians.peds:
            _ped.ch.y = gy(_ped.ch.x, _ped.ch.z)
        pr.begin_mode_3d(self.camera.camera)
        self.pedestrians.draw(self.registry)
        if show_intern:
            self.intern.draw(self.registry)
            # same cyan ring + "!" diamond as every quest stop (one shared impl)
            self.park.draw_quest_indicator(self.intern.x, self.intern.z,
                                           self.intern.height + 1.4)
        if show_bob:
            self.bob.draw(self.registry)
            self.park.draw_quest_indicator(self.bob.x, self.bob.z, self.bob.height + 1.4)
        if show_lady:
            self.lady.draw(self.registry)
            self.park.draw_quest_indicator(self.lady.x, self.lady.z, self.lady.height + 1.4)
        if show_civilian:
            self.civilian.draw(self.registry)
            if self._pet_stage in (0, 2):        # "!" when he wants to talk (offer / collect)
                self.park.draw_quest_indicator(self.civilian.x, self.civilian.z,
                                               self.civilian.height + 1.4)
        if show_pet:
            self.pet.draw(self.registry)
            if self._pet_stage == 1:             # "!" over Biscuit while he's lost
                self.park.draw_quest_indicator(self.pet.x, self.pet.z, self.pet.height + 1.0)
        if show_busker:
            self.busker.draw(self.registry)
            if self._guitar_stage in (0, 2):     # "!" when Río wants to talk
                self.park.draw_quest_indicator(self.busker.x, self.busker.z,
                                               self.busker.height + 1.4)
        if show_guitar:                          # the stashed guitar (a simple prop)
            self._draw_guitar_prop()
            self.park.draw_quest_indicator(self._guitar_pos[0], self._guitar_pos[1], 1.4)
        if self.robin_following:
            self.robin.draw(self.registry)
        if guide is not None:                    # the gold "go here" beacon for your picked to-do
            self.park.draw_guide_beacon(guide[0], guide[1])
        ceo.draw(self.registry)
        pr.end_mode_3d()
        self._draw_park_overlay(near, near_npc)
        if guide is not None:                    # top chip + screen-edge arrow to the spot
            self._draw_guide_hud(guide)
        self._draw_companion_chip()
        if self._bob_talk is not None:           # childhood friend's greeting, on top
            self._draw_bob_talk()
        if self._lady_talk is not None:          # Mae's conversation, on top
            self._draw_lady_talk()
        if self._civilian_talk is not None:      # Walter's conversation, on top
            self._draw_civilian_talk()
        if self._busker_talk is not None:        # Río's conversation, on top
            self._draw_busker_talk()
        self._draw_park_toast()
        if self._quest_input is not None:        # quest-stop decision capture, on top
            self._draw_quest_input()
        self._do_dossier_action(self.dossier.draw(self.company))
        self._do_investor_action(self.investor.draw(self.company, self._rounds_raised()))
        if self.shop.open:                     # Bolt Hardware: furnish your office from the city
            action = self.shop.draw(self.cash)
            if action == "close":
                self.shop.close()
            elif isinstance(action, tuple) and action[0] == "buy":
                self.buy_item(action[1])
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

    def _clock_state(self) -> dict:
        """Snapshot for the phone's Clock app: in-game time to the minute, the date,
        the day/night phase and the season. The daylight clock (0..day_seconds) maps
        onto a 24h day, with clock 0 = 00:00 (Midnight)."""
        d = self.daylight
        frac = (d.clock / d.day_seconds) % 1.0
        total_min = int(frac * 24 * 60)
        cal = self.calendar
        return {
            "hh": total_min // 60, "mm": total_min % 60,
            "weekday": cal.weekday,
            "date": cal.label(),
            "day_number": cal.day_number,
            "phase": d.phase_name,
            "season": self.season.name,
        }

    def _draw_companion_chip(self) -> None:
        """Top-center chip confirming Robin is walking with you (office + park),
        with the reminder of how to call it off. Hidden when he's not following."""
        if not self.robin_following:
            return
        label = f"{COFOUNDER_NAME} is following  ·  Nokia ▸ Co-founder to stop"
        w = pr.measure_text(label, 16)
        x = config.WINDOW_WIDTH // 2 - w // 2
        pr.draw_rectangle(x - 10, 46, w + 20, 26, pr.Color(28, 86, 104, 210))
        pr.draw_text(label, x, 50, 16, pr.Color(190, 235, 245, 255))

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
                elif pending[0] == "raise_round":   # the VC firm: a meeting, not a to-do
                    sub = "Press E: pitch investors"
                    sub_col = pr.Color(120, 215, 235, 255)
                else:
                    task = tasks.TASK_BY_KEY.get(pending[0])
                    sub = f"TO-DO: {task.title}" if task else "Press E"
                    sub_col = pr.Color(120, 215, 235, 255)
            elif n.is_store:
                sub = {"hire": "Press E: hire talent",
                       "shop": "Press E: furnish your office"}.get(
                           n.store, "Press E: change your outfit")
                sub_col = pr.Color(210, 175, 235, 255)
            self._label_3d(n.name, sub, sub_col, n.x, min(self.park.top_of(n), 6.0), n.z, 14,
                           main_color=pr.Color(240, 226, 180, 255))

        # City parks: a soft green name label over each lawn when you're near it.
        for p in self.park.parks:
            if math.hypot(p.x - ceo.x, p.z - ceo.z) > LABEL_DIST:
                continue
            self._label_3d(p.name, "City Park", pr.Color(150, 230, 175, 255),
                           p.x, 3.6, p.z, 16, main_color=pr.Color(170, 235, 190, 255))

        # The park intern: name + action label under the shared quest indicator.
        if self._intern_park is not None and not self.taskboard.is_done("intern"):
            it = self.intern
            inn = math.hypot(it.x - ceo.x, it.z - ceo.z) <= parkmod.REACH
            sub = "Press E: join your team (free)" if inn else "Your first intern"
            self._label_3d(it.name, sub, pr.Color(150, 230, 175, 255),
                           it.x, it.height + 0.4, it.z, 16,
                           main_color=pr.Color(170, 235, 190, 255))

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

        # interaction prompt — lease lots take priority, else a quest-stop offer.
        prompt, afford = None, True
        if near is not None:
            if near.leased:
                prompt = f"Press  E  to enter {near.name}"
            else:
                prompt = f"Press  E  to lease {near.name}   -   Deposit ${near.deposit:,}  ·  Rent ${near.rent:,}/mo"
                afford = self.cash >= near.deposit
        elif near_npc is not None and near_npc.is_store:
            verb = {"hire": "hire agents", "shop": "furnish your office"}.get(
                near_npc.store, "change your outfit")
            prompt = f"Press  E  at {near_npc.name}  to {verb}"
        elif near_npc is not None:
            pending = near_npc.pending(self.taskboard.done)
            if not pending:
                prompt = f"{near_npc.name} — nothing left to do here"
            elif len(near_npc.task_keys()) > 1:        # workshop: the canvas
                prompt = (f"Press  E  at {near_npc.name}  to work your business model "
                          f"canvas   -   {len(pending)} left")
            elif pending[0] == "raise_round":          # the VC firm: pitch, not a to-do
                prompt = f"Press  E  at {near_npc.name}  to pitch investors"
            else:
                task = tasks.TASK_BY_KEY.get(pending[0])
                bonus = f"   -   +${near_npc.reward:,} seed money" if near_npc.reward else ""
                title = task.title.lower() if task else "help out"
                prompt = f"Press  E  at {near_npc.name}  to {title}{bonus}"
        if prompt is not None:
            tw = pr.measure_text(prompt, 20)
            x = (config.WINDOW_WIDTH - tw) // 2
            y = config.WINDOW_HEIGHT - 70
            pr.draw_rectangle(x - 14, y - 8, tw + 28, 36, pr.Color(0, 0, 0, 170))
            pr.draw_text(prompt, x, y, 20, pr.RAYWHITE if afford else pr.Color(230, 140, 140, 255))
        pr.draw_text("WASD move  -  E lease / enter / shop  -  P office  -  "
                     "C company  -  N phone",
                     18, config.WINDOW_HEIGHT - 28, 18, pr.LIGHTGRAY)

    def _sync_city_geo(self) -> None:
        """Push live positions of the CEO, every agent, and the city's shops into a
        Redis geospatial index once a second. Lets the backend ask spatial questions
        (nearest idle engineer, who's by the cafe) without the game scanning entities
        each frame. Best-effort + gated on REDIS_URL — never disturbs the game loop."""
        now = pr.get_time()
        if now - getattr(self, "_geo_last", 0.0) < 1.0:
            return
        self._geo_last = now
        try:
            from backend import city_geo
            if not city_geo.is_configured():
                return
            ceo = self.player.ch
            ents = [{"id": "ceo", "x": ceo.x, "z": ceo.z, "kind": "agent",
                     "name": ceo.name or "You (CEO)", "role": "CEO"}]
            for a in self.all_agents:
                ents.append({"id": a.backend_id or f"agent:{a.name}",
                             "x": a.x, "z": a.z, "kind": "agent",
                             "name": a.name, "role": a.role})
            for b in self.park.npc:
                ents.append({"id": b.id, "x": b.x, "z": b.z,
                             "kind": "building", "name": b.name})
            city_geo.sync(ents)
        except Exception:
            pass

    def run(self) -> None:
        pr.set_config_flags(pr.FLAG_MSAA_4X_HINT)
        pr.init_window(config.WINDOW_WIDTH, config.WINDOW_HEIGHT, config.WINDOW_TITLE)
        pr.set_target_fps(config.TARGET_FPS)
        pr.set_exit_key(pr.KEY_NULL)   # Esc must NOT quit the game; it only closes the chat
        # Tighten the depth range. raylib's default near=0.01 wrecks depth precision
        # in the distance, so coplanar building geometry (window panes vs walls) and
        # the street z-fight/flicker far from the camera. The camera never sits closer
        # than ~4 units to its target and nothing draws past the ~62-unit cull, so a
        # near of 0.5 / far of 250 is safe and gives ~100x better far-depth precision.
        pr.rl_set_clip_planes(0.5, 250.0)
        shop_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 90, 210, 50, "Shop  (B)")
        meeting_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 154, 210, 50, "Meeting  (G)")
        park_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 218, 210, 50, "Office Park  (P)")
        files_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 282, 210, 50, "Files  (V)")
        jobs_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 346, 210, 50, "Jobs  (J)")
        phone_btn = Button(config.WINDOW_WIDTH - 230, config.WINDOW_HEIGHT - 410, 210, 50, "Phone  (N)")

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
            rent = self.park.tick_rent(dt)
            self.cash -= rent
            # The idle market + farm tick everywhere too — money grows while you roam.
            self.market.update(dt)
            self.farm.update(dt)
            if rent:                    # once per in-game month: checkpoint both
                self.market.save(self.link)   # (stamps last_seen for offline catch-up)
                self.farm.save(self.link)

            # Checkpoint cash + leased offices (throttled inside) so the balance and
            # your buildings survive a restart, not just the market/roster.
            self._persist_state()
            # If you've gone flat broke, Bob texts you to meet up for an emergency stake.
            self._check_bob_rescue()

            # Advance the day/night cycle and feed it to the character shader (the
            # sky color is read per-frame in each draw path). T peeks at the next
            # phase without waiting for the clock.
            self.calendar.advance(self.daylight.advance(dt))
            if pr.is_key_pressed(pr.KEY_T):
                self.calendar.advance(self.daylight.skip_phase())
            self.registry.set_daylight(self.daylight)

            # Seasons drift much slower than the day; trees swap foliage as it turns.
            self.season.set_day(self.calendar.day)  # season tracks the calendar (7 days each)

            # Release agents whose background reply has landed, even if their chat
            # panel is closed — otherwise they stay stuck showing "working".
            self._reconcile_busy_agents()

            # Ambient inbox: agents drop the odd status update, park businesses
            # (NPCs) reach out. Real "finished work" messages come from above.
            self.inbox_feeder.tick(dt, self.all_agents,
                                   [n.name for n in self.park.npc],
                                   self.inbox, pr.get_time())

            # Keep the Redis geo map of the living city fresh (CEO, agents, shops) so
            # the backend can answer "who/what is near here?" at Redis speed. Throttled
            # and fully gated — a Redis hiccup never reaches the render loop.
            self._sync_city_geo()

            if self._e_cooldown > 0:        # swallow an E press that lingers across
                self._e_cooldown -= 1       # a mode/room switch (no EndDrawing between)

            self._refresh_tasks()           # tick the plot's auto-completing to-dos
            # The to-do list lives only on the phone now (N → To-Do) — no L panel.
            # C toggles the Company Dossier (view/edit the decisions agents read).
            if pr.is_key_pressed(pr.KEY_C) and not self.chat.open \
                    and not self.dossier.capturing and not self.market_panel.open \
                    and not self.grant_panel.open and not self.slot_panel.open \
                    and not self.farm_panel.open:
                self.dossier.toggle()
            # N toggles the Nokia phone from anywhere (office OR city) — press again to
            # close. Skipped while a text field owns the keyboard (incl. the phone's own
            # message screens, where N should type a letter, not slam the phone shut).
            if pr.is_key_pressed(pr.KEY_N) and not self.chat.open \
                    and not self.dossier.capturing and not self.market_panel.open \
                    and not self.grant_panel.open and not self.slot_panel.open \
                    and not self.farm_panel.open \
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
            elif self.elevator_open or self.shop.open:
                pass  # the modal handles its own input inside draw()
            elif (self.dossier.open or self.investor.open or self.market_panel.open
                  or self.grant_panel.open or self.slot_panel.open
                  or self.farm_panel.open):
                pass  # a full-screen panel is up; it's modal — freeze movement/keys
            elif self._quest_input is not None:
                pass  # talking to a quest NPC; the dialogue modal owns the keyboard
            else:
                self.player.update(dt, self.camera, self.characters)
                self.camera.update(dt, self.player.ch)
                if gamepad.pressed(gamepad.DPAD_RIGHT) or pr.is_key_pressed(pr.KEY_TAB):
                    self.cycle_selection(1)
                elif gamepad.pressed(gamepad.DPAD_LEFT):
                    self.cycle_selection(-1)
                if pr.is_key_pressed(pr.KEY_B) or gamepad.pressed(gamepad.DPAD_UP):
                    self.shop.open_()
                if pr.is_key_pressed(pr.KEY_G) and len(self.agents) >= 2:
                    self.meeting.open_panel()
                if pr.is_key_pressed(pr.KEY_P):
                    self._enter_park()
                if pr.is_key_pressed(pr.KEY_V):
                    self.drive.open_panel()
                if pr.is_key_pressed(pr.KEY_J):
                    self.jobs.open_panel()
                # Left-click an agent to select it (ignore clicks on the HUD buttons).
                m = pr.get_mouse_position()
                if pr.is_mouse_button_pressed(pr.MOUSE_BUTTON_LEFT) \
                        and not pr.check_collision_point_rec(m, shop_btn.rect) \
                        and not pr.check_collision_point_rec(m, meeting_btn.rect) \
                        and not pr.check_collision_point_rec(m, park_btn.rect) \
                        and not pr.check_collision_point_rec(m, files_btn.rect) \
                        and not pr.check_collision_point_rec(m, jobs_btn.rect) \
                        and not pr.check_collision_point_rec(m, phone_btn.rect):
                    picked = self._pick_agent()
                    if picked >= 0:
                        self.selected = picked
                target = self._talk_target()
                if target and (pr.is_key_pressed(pr.KEY_F) or gamepad.pressed(gamepad.TRIANGLE)):
                    self.chat.open_with(target)
                    self._freeze_chat_target(target)
                # Walk up to the records cabinet and press E to open the dossier;
                # otherwise E uses a nearby portal (doorway / elevator / exit).
                portal = self._nearest_portal()
                if pr.is_key_pressed(pr.KEY_E) and self._e_cooldown == 0:
                    if self._near_quest_actor():        # talk to the NPC inside
                        b = self._quest_building
                        if b.is_store:                  # shop NPC greets you, then opens
                            self._open_store_greeting(b)
                        else:                           # quest stop → the dialogue/ask
                            self._visit_quest_stop(b)
                    elif self._quest_building is None and self._near_records():
                        if not self.dossier.open:       # Files cabinet (your office only)
                            self.dossier.toggle()
                        self._e_cooldown = 4
                    elif portal is not None:
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
                for brain in self._active_brains():
                    brain.update(dt)
                self._greet_at_reception()      # front-desk hello on walk-up

            for ch in self.characters:
                ch.update(dt, self.registry)
            self._update_companion(dt)      # Robin trails you around the office too

            pr.begin_drawing()
            pr.clear_background(self.daylight.sky_color())  # time-of-day sky

            sel = self.agents[self.selected] if 0 <= self.selected < len(self.agents) else None
            # Robin isn't part of the roster (no desk/brain/selection), so he's drawn
            # alongside the room's characters only while he's following you.
            draw_chars = self.characters + [self.robin] if self.robin_following else self.characters
            self.scene.draw_world(draw_chars, self.registry, self.camera.camera, sel)
            self._draw_portals_3d(self.camera.camera)
            draw_world_labels(draw_chars, self.camera.camera)
            self._draw_agent_status()
            self._draw_meeting_badges()
            self._draw_bubbles()
            self._draw_companion_chip()

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
            elif self.shop.open:
                action = self.shop.draw(self.cash)
                if action == "close":
                    self.shop.close()
                elif isinstance(action, tuple) and action[0] == "buy":
                    self.buy_item(action[1])
            elif self._quest_building is not None:
                self._draw_quest_building_hud()
            else:
                draw_hud(self.company_name, self.cash, len(self.agents), config.HIRE_COST, sel)
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
                self._draw_room_label()
                self._draw_talk_prompt()
                portal = self._nearest_portal()
                if self._near_records():
                    self._draw_records_prompt()
                elif portal is not None:
                    self._draw_portal_prompt(portal)
                self._do_dossier_action(self.dossier.draw(self.company))

            pr.end_drawing()

        self.market.save(self.link)        # checkpoint the portfolio + last_seen on quit
        self.farm.save(self.link)          # checkpoint the farm + last_seen on quit
        self._persist_state(force=True)    # flush cash + leases on quit
        self.chat.voice.shutdown()
        self.meeting_link.shutdown()
        self.coordinator.shutdown()
        if self._dispatcher is not None:
            self._dispatcher.stop()
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
        # Drain finished firehose tasks (posted from the Dispatcher's worker threads)
        # into the inbox here, on the render thread.
        while True:
            try:
                task, result, agent = self._task_results.get_nowait()
            except queue.Empty:
                break
            who = agent.name if agent is not None else "Team"
            self.inbox.post(who, result, kind="agent",
                            subject=f"Task: {_inbox_short(str(task.get('text', '')), 24)}",
                            agent_id=(agent.id if agent is not None else None),
                            ts=pr.get_time())

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
    from game import npc_validate
    npc_validate.report()          # warn on park_lots ↔ tasks ↔ dialogue drift (non-fatal)
    Game().run()

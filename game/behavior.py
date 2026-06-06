"""Bot brains: the per-agent behaviour state machine + the office Director.

This is the *reactive* tier of the two-tier design. The LLM (separately) authors
a BotPolicy — a route, three personality knobs, and a banter pool. This module
*runs* that policy every frame for free: it steers the character along navgrid
paths, decides when to idle vs. roam vs. socialize, obeys CEO commands, and pops
speech bubbles. No model calls happen here.

Priority of intents (higher preempts lower):
    USER command  >  MEETING  >  autonomous routine (roam/socialize/work)

Cost control lives in the Director: it only *nudges* idle bots on a slow tick,
weighted by their knobs, so an office full of agents stays lively without the
model ever being in the loop.

No raylib dependency — headlessly testable.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from . import zones, locomotion

# --- states -----------------------------------------------------------------
WORK = "work"            # idle at own desk
ROAM = "roam"            # walking the policy route
SOCIALIZE = "socialize"  # visiting another bot for banter
GOTO = "goto"            # CEO told it to go somewhere
FOLLOW = "follow"        # CEO told it to follow
MEETING = "meeting"      # summoned to the meeting table

# --- command priorities -----------------------------------------------------
P_AUTO = 10
P_MEETING = 50
P_USER = 100


@dataclass
class BotPolicy:
    """What the LLM authors (defaults let bots live before any model call)."""
    home: tuple                       # (x, z) stand spot in front of the desk
    route: list = field(default_factory=list)   # zone names to patrol
    sociability: float = 0.4          # 0..1 chance-weight to seek out peers
    restlessness: float = 0.4         # 0..1 chance-weight to leave the desk
    focus: float = 0.6                # 0..1 -> longer dwell at the desk
    banter: list = field(default_factory=list)   # persona one-liners
    mood: str = "neutral"


@dataclass
class Command:
    kind: str               # goto | follow | talk_to | gather | back_to_work
    priority: int = P_USER
    target: object = None   # zone name, (x,z) point, or a Character
    sit: bool = False       # gather: sit on the stool (vs. stand) once arrived


@dataclass
class BotContext:
    """Shared world handles the brains read (live references, not copies)."""
    nav: object             # NavGrid
    ceo: object             # CEO Character (for FOLLOW)
    agents: list            # all agent Characters (peers = these minus self)
    seats: dict = field(default_factory=dict)  # zone name -> ((x, z), yaw) seatable spot


def default_policy(ch, home: tuple) -> BotPolicy:
    """A reasonable policy before the planner runs, varied per-character so bots
    don't all behave identically. Seeded by name for determinism."""
    rng = random.Random(hash(ch.name) & 0xFFFFFFFF)
    route = zones.all_names()
    rng.shuffle(route)
    return BotPolicy(
        home=home,
        route=route[:3],
        sociability=round(rng.uniform(0.25, 0.7), 2),
        restlessness=round(rng.uniform(0.25, 0.65), 2),
        focus=round(rng.uniform(0.4, 0.85), 2),
        banter=[],
        mood="neutral",
    )


class BotBrain:
    SOCIAL_GAP = 1.3        # how close to stand when visiting a peer
    FOLLOW_GAP = 1.8        # trailing distance when following the CEO
    REPATH_MOVE = 1.0       # CEO must move this far before we re-path (follow)

    def __init__(self, ch, policy: BotPolicy, ctx: BotContext) -> None:
        self.ch = ch
        self.policy = policy
        self.ctx = ctx
        self.state = WORK
        self.follower = locomotion.PathFollower()
        self.cmd: Command | None = None
        self.frozen = False           # held in place (e.g. while the CEO chats with it)
        self.dwell = 0.0              # seconds left to idle at the current spot
        self.bubble: str | None = None
        self._bubble_t = 0.0
        self._meet_timer = 0.0        # countdown to the next meeting-table banter line
        self._seated = False          # currently sitting (desk chair or couch)
        self._pending_sit = None      # yaw to face once we arrive at a seat, else None
        self._route_i = 0
        self._last_ceo = None
        self._rng = random.Random((hash(ch.name) ^ 0x5bd1e995) & 0xFFFFFFFF)

    # -- public API the game / director / chat use ---------------------------
    def command(self, kind: str, target=None, priority: int = P_USER,
                sit: bool = False) -> None:
        """Queue a command; it preempts the autonomous routine. A lower-priority
        command (e.g. a meeting gather) won't override a higher one already active
        (e.g. a CEO 'follow me'): USER > MEETING > auto."""
        if self.cmd is not None and self.cmd.priority > priority:
            return
        self.cmd = Command(kind=kind, target=target, priority=priority, sit=sit)
        self._begin_command()

    def say(self, text: str, secs: float = 3.5) -> None:
        self.bubble, self._bubble_t = text, secs

    @property
    def commanded(self) -> bool:
        return self.cmd is not None

    def start_roam(self) -> None:
        if self.frozen or self.commanded:
            return
        self._route_i = (self._route_i + 1) % max(1, len(self.policy.route))
        dest = self.policy.route[self._route_i] if self.policy.route else "meeting"
        seat = self.ctx.seats.get(dest)          # a couch/seat at this zone?
        point = seat[0] if seat else zones.point(dest)
        if self._go_to(point):
            self.state = ROAM
            self._pending_sit = seat[1] if seat else None

    def start_socialize(self) -> None:
        if self.frozen or self.commanded:
            return
        peer = self._pick_peer()
        if peer is None:
            return self.start_roam()
        if self._go_to(self._spot_near(peer, self.SOCIAL_GAP)):
            self.state = SOCIALIZE
            self._social_peer = peer

    # -- per-frame update ----------------------------------------------------
    def update(self, dt: float) -> None:
        self._tick_bubble(dt)
        if self.frozen:
            locomotion.apply_anim(self.ch, moving=False)
            return

        if self.cmd is not None:
            self._update_command(dt)
            return

        # Working-state rule: a bot actually busy on a backend task sits at its
        # desk and won't wander. Movement on screen => the agent is free.
        if self.ch.status == "working" and self.state != WORK:
            self._go_work()

        if self.state == WORK:
            self._update_dwell(dt)            # just stand there until nudged
        elif self.state in (ROAM, SOCIALIZE):
            self._update_routine(dt)

    # -- command handling ----------------------------------------------------
    def _begin_command(self) -> None:
        c = self.cmd
        if c.kind == "back_to_work":
            self.cmd = None
            self._go_work()
        elif c.kind == "follow":
            self.state = FOLLOW
            self._last_ceo = (self.ctx.ceo.x, self.ctx.ceo.z)
            self._go_to(self._spot_near(self.ctx.ceo, self.FOLLOW_GAP))
            self.say("On my way — following you.")
        elif c.kind in ("goto", "gather"):
            pt = self._resolve_point(c.target)
            self.state = MEETING if c.kind == "gather" else GOTO
            self._go_to(pt)
            self.say("Heading to the meeting." if c.kind == "gather" else "On it.")
            if c.kind == "gather":
                # Stagger first table-banter so they don't all talk on arrival.
                self._meet_timer = self._rng.uniform(1.0, 5.0)
        elif c.kind == "talk_to":
            peer = c.target
            self.state = SOCIALIZE
            self._social_peer = peer
            self._go_to(self._spot_near(peer, self.SOCIAL_GAP))
            self.say(f"Going to talk to {getattr(peer, 'name', 'them')}.")

    def _update_command(self, dt: float) -> None:
        c = self.cmd
        if c.kind == "follow":
            ceo = self.ctx.ceo
            moved = self._last_ceo is None or math.hypot(
                ceo.x - self._last_ceo[0], ceo.z - self._last_ceo[1]) > self.REPATH_MOVE
            far = math.hypot(ceo.x - self.ch.x, ceo.z - self.ch.z) > self.FOLLOW_GAP + 0.4
            if moved and far:
                self._last_ceo = (ceo.x, ceo.z)
                self._go_to(self._spot_near(ceo, self.FOLLOW_GAP))
            self.follower.update(self.ch, dt)
            return  # follow only ends on a new command (e.g. back_to_work)

        # goto / gather / talk_to: drive the path, then settle.
        if self.follower.active:
            self.follower.update(self.ch, dt)
            return
        if c.kind == "gather":
            # Sit on the stool (or stand if none), turned toward the NEAREST table
            # (a plan may have several), until released (back_to_work).
            centers = zones.meeting_centers()
            if centers:
                cx, cz = min(centers, key=lambda p: (p[0] - self.ch.x) ** 2
                             + (p[1] - self.ch.z) ** 2)
                locomotion.face_dir(self.ch, cx - self.ch.x, cz - self.ch.z, 1.0)
            locomotion.apply_anim(self.ch, moving=False, seated=c.sit)
            # Chime in now and then so the table looks like a live discussion.
            self._meet_timer -= dt
            if self._meet_timer <= 0.0:
                self.say(self._banter_line(), secs=3.0)
                self._meet_timer = self._rng.uniform(4.0, 9.0)
            return
        if c.kind == "goto":
            # A direct order: stay put until recalled ("back to work").
            locomotion.apply_anim(self.ch, moving=False)
            return
        # talk_to is a one-shot visit: say a line, then resume the routine.
        if c.kind == "talk_to":
            self._face(self._social_peer)
            self.say(self._banter_line())
        self.cmd = None
        self._go_work()

    # -- autonomous routine --------------------------------------------------
    def _update_routine(self, dt: float) -> None:
        if self.follower.active:
            self.follower.update(self.ch, dt)
            if not self.follower.active:           # just arrived
                self.dwell = self._dwell_secs()
                if self._pending_sit is not None:  # a couch/seat — sit down
                    self._seated = True
                    self.ch.yaw = self._pending_sit
                    self._pending_sit = None
                elif self.state == SOCIALIZE:
                    self._face(getattr(self, "_social_peer", None))
                    self.say(self._banter_line())
            return
        # arrived: dwell (seated if it's a seat), then head home
        self.dwell -= dt
        locomotion.apply_anim(self.ch, moving=False, seated=self._seated)
        if self.dwell <= 0.0:
            self._go_work()

    def _update_dwell(self, dt: float) -> None:
        # Walk back to the desk if displaced; otherwise sit at it and work.
        if self.follower.active:
            self.follower.update(self.ch, dt)
            return
        if not self._seated:                       # settle into the desk chair
            self._seated = True
            if self.ch.desk is not None:
                self.ch.yaw = math.degrees(math.atan2(
                    self.ch.desk[0] - self.ch.x, self.ch.desk[1] - self.ch.z))
        locomotion.apply_anim(self.ch, moving=False, seated=self._seated)

    # -- helpers -------------------------------------------------------------
    def _go_work(self) -> None:
        self.state = WORK
        self._go_to(self.policy.home)

    def _go_to(self, point) -> bool:
        self._seated = False          # any new walk stands the bot up first
        self._pending_sit = None      # default: arrive standing unless a seat sets it
        if point is None:
            self.follower.clear()
            return False
        path = self.ctx.nav.find_path((self.ch.x, self.ch.z), point)
        self.follower.set_path(path)
        return bool(path)

    def _resolve_point(self, target):
        if isinstance(target, str):
            return zones.point(target)
        if isinstance(target, (tuple, list)) and len(target) == 2:
            return (float(target[0]), float(target[1]))
        if target is not None and hasattr(target, "x"):
            return self._spot_near(target, self.SOCIAL_GAP)
        return zones.point("meeting")

    def _spot_near(self, other, gap: float):
        """A point `gap` units from `other`, on the line toward this bot."""
        dx, dz = self.ch.x - other.x, self.ch.z - other.z
        d = math.hypot(dx, dz) or 1.0
        return (other.x + dx / d * gap, other.z + dz / d * gap)

    def _pick_peer(self):
        peers = [a for a in self.ctx.agents
                 if a is not self.ch and a.status != "working"]
        return self._rng.choice(peers) if peers else None

    def _face(self, other) -> None:
        if other is not None:
            locomotion.face_dir(self.ch, other.x - self.ch.x, other.z - self.ch.z, 1.0)

    def _banter_line(self) -> str:
        if self.policy.banter:
            return self._rng.choice(self.policy.banter)
        return self._rng.choice([
            "How's the project going?", "Coffee?", "Big day today.",
            "Let me know if you need anything.", "Back to it soon.",
        ])

    def _dwell_secs(self) -> float:
        # More focus -> shorter loitering away from the desk.
        return self._rng.uniform(1.5, 4.0) * (1.2 - 0.6 * self.policy.focus)

    def _tick_bubble(self, dt: float) -> None:
        if self.bubble is not None:
            self._bubble_t -= dt
            if self._bubble_t <= 0.0:
                self.bubble = None


class Director:
    """Low-frequency ambient scheduler: nudges idle bots to roam/socialize based
    on their personality knobs. This is where 24/7 liveliness comes from without
    any model calls. (The LLM token budget lives here too, used by the planner.)"""

    TICK = 0.8   # seconds between decision passes

    def __init__(self, brains: list, llm_budget_per_min: int = 6) -> None:
        self.brains = brains
        self._acc = 0.0
        self.llm_budget_per_min = llm_budget_per_min
        self._llm_window = 0.0
        self._llm_used = 0

    def tick(self, dt: float) -> None:
        # refill the per-minute LLM budget (consumed by the planner in step 4)
        self._llm_window += dt
        if self._llm_window >= 60.0:
            self._llm_window = 0.0
            self._llm_used = 0

        self._acc += dt
        if self._acc < self.TICK:
            return
        self._acc = 0.0
        for b in self.brains:
            if b.frozen or b.commanded or b.state != WORK:
                continue
            if b.ch.status == "working":      # busy bots stay at their desk
                continue
            r = b._rng.random()
            roam_p = b.policy.restlessness * 0.5
            social_p = b.policy.sociability * 0.4
            if r < roam_p:
                b.start_roam()
            elif r < roam_p + social_p:
                b.start_socialize()

    # -- LLM budget gate (used by the async planner) -------------------------
    def can_spend_llm(self) -> bool:
        return self._llm_used < self.llm_budget_per_min

    def note_llm_spend(self) -> None:
        self._llm_used += 1

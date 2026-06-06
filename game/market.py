"""A fictional idle stock market — the core of the bank/broker minigame.

This module is pure logic + persistence (no rendering; see game/market_panel.py).
The fantasy: park money in assets, they tick up (or down) in real time, you
reinvest the gains. Two venues split the risk ladder:

  * the BANK  — Savings (steady interest) + safe instruments (T-bonds, an index
    fund). Reliable passive flow.
  * the BROKER — fictional individual stocks and a crypto sector: high variance,
    big swings, the gamble tier (each with a bit of lore so picking feels flavoured).

Prices do a per-tick random walk (drift = mean return, vol = swing). A light news
engine fires the occasional bull run / crash on a sector — diversification (the
index) rides them out better than a single meme stock. When you've been away, a
catch-up advances the missed ticks so you come back to a "while you were away"
payout — the return-and-collect moment that makes idle games sticky.

Cash itself lives on the Game (self.cash); the Market only holds your SAVINGS
balance and your share HOLDINGS. Buy/sell return the cash delta for the caller to
apply, so there's a single source of truth for money.

Deeper layers from the design (DRIP/auto-reinvest, robo-advisor automation,
research/upgrade trees, exotic leveraged/options tiers, a prestige "IPO a bigger
fund" reset) are intentionally left as follow-ons — the asset list + tick model
here are built to extend.
"""
from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass, field

TICK_SECONDS = 2.0              # real seconds per market tick while playing
OFFLINE_CAP_SECONDS = 8 * 3600  # cap "while you were away" at 8 hours of ticks
SAVINGS_RATE_PER_TICK = 0.0032  # ~10% per in-game month (30 ticks) on savings
NEWS_MIN_TICKS = 22             # roughly how often a market event fires
BUY_CHUNK = 1000                # default trade size (dollars) for the panel buttons


@dataclass
class Asset:
    id: str
    name: str
    venue: str          # "bank" or "broker" — which building lists it
    tier: str           # "bond" | "index" | "stock" | "crypto" (risk ladder)
    blurb: str          # one-line lore / flavour
    price: float
    drift: float        # mean return per tick
    vol: float          # stddev of the per-tick return (its volatility)
    prev: float = 0.0   # last tick's price (for the change %)

    @property
    def change_pct(self) -> float:
        return 0.0 if self.prev <= 0 else (self.price / self.prev - 1.0) * 100.0


# The catalogue. Tuned so the bank trickles reliably and the broker swings hard.
def _catalog() -> list[Asset]:
    return [
        # --- BANK: the safe, steady tiers --------------------------------------
        Asset("tbond", "Treasury Bills", "bank", "bond",
              "Boring on purpose. A dribble of yield that never blinks.",
              price=100.0, drift=0.0008, vol=0.0012),
        Asset("index", "Broad Market Index", "bank", "index",
              "Owns a slice of everything. Slow, steady, hard to kill.",
              price=250.0, drift=0.0026, vol=0.012),
        # --- BROKER: the gamble tiers ------------------------------------------
        Asset("voltride", "Voltride Motors", "broker", "stock",
              "Plucky EV upstart. Always one tweet from the moon or the floor.",
              price=72.0, drift=0.004, vol=0.052),
        Asset("mememunch", "MemeMunch Foods", "broker", "stock",
              "Viral snack chain. Trades entirely on vibes and hashtags.",
              price=33.0, drift=0.0028, vol=0.085),
        Asset("genetwist", "GeneTwist Bio", "broker", "stock",
              "Shady biotech. One trial away from 10x — or a smoking crater.",
              price=18.0, drift=0.0016, vol=0.13),
        Asset("shibamax", "ShibaMax", "broker", "crypto",
              "A fictional dog coin. No earnings, no floor, no ceiling. Pure chaos.",
              price=4.0, drift=0.006, vol=0.22),
    ]


@dataclass
class Market:
    assets: dict           # id -> Asset (live)
    holdings: dict         # id -> shares owned
    savings: float = 0.0
    tick_accum: float = 0.0
    ticks: int = 0
    last_seen: float = 0.0     # wall-clock of last save (for offline catch-up)
    news: str = "Markets open. Money never sleeps."
    _news_cooldown: int = NEWS_MIN_TICKS
    away: dict | None = None   # last "while you were away" summary (consumed by UI)

    # ---- construction / persistence --------------------------------------
    @classmethod
    def fresh(cls) -> "Market":
        assets = {a.id: a for a in _catalog()}
        for a in assets.values():
            a.prev = a.price
        return cls(assets=assets, holdings={a: 0.0 for a in assets})

    @classmethod
    def load(cls, link) -> "Market":
        """Restore from the save (settings blob), else a fresh market. Applies an
        offline catch-up so prices/savings advance for time spent away."""
        m = cls.fresh()
        try:
            raw = link.load_market()
        except Exception:
            raw = None
        if raw:
            for aid, ap in raw.get("assets", {}).items():
                if aid in m.assets:
                    m.assets[aid].price = float(ap.get("price", m.assets[aid].price))
                    m.assets[aid].prev = float(ap.get("prev", m.assets[aid].price))
            m.holdings.update({k: float(v) for k, v in raw.get("holdings", {}).items()
                               if k in m.assets})
            m.savings = float(raw.get("savings", 0.0))
            m.ticks = int(raw.get("ticks", 0))
            m.last_seen = float(raw.get("last_seen", 0.0))
        m._catch_up()
        return m

    def to_dict(self) -> dict:
        return {
            "assets": {a.id: {"price": a.price, "prev": a.prev}
                       for a in self.assets.values()},
            "holdings": self.holdings,
            "savings": self.savings,
            "ticks": self.ticks,
            "last_seen": time.time(),
        }

    def save(self, link) -> None:
        try:
            link.save_market(self.to_dict())
        except Exception:
            pass

    # ---- the tick model --------------------------------------------------
    def _step_prices(self) -> None:
        """One market tick: random-walk every price, accrue savings interest."""
        for a in self.assets.values():
            a.prev = a.price
            shock = a.drift + a.vol * random.gauss(0.0, 1.0)
            a.price = max(0.01, a.price * (1.0 + shock))
        self.savings *= (1.0 + SAVINGS_RATE_PER_TICK)
        self.ticks += 1
        self._maybe_news()

    def _maybe_news(self) -> None:
        """Occasionally fire a sector event — a one-off shock + a ticker headline.
        The index shrugs most of these off; single stocks take the full hit."""
        self._news_cooldown -= 1
        if self._news_cooldown > 0:
            return
        self._news_cooldown = NEWS_MIN_TICKS + random.randint(0, 18)
        events = [
            ("crypto", 1.35, "📈 ShibaMax goes parabolic — crypto bros rejoice."),
            ("crypto", 0.6, "📉 Crypto rugpull! ShibaMax craters overnight."),
            ("stock", 1.22, "📈 Retail frenzy: meme stocks rip on social buzz."),
            ("stock", 0.78, "📉 Earnings miss — a hyped stock gets gutted."),
            ("index", 1.06, "📈 Bull run: the broad market grinds to new highs."),
            ("index", 0.9, "📉 Selloff: broad market dips on rate fears."),
            ("stock", 0.55, "💥 BLACK SWAN: GeneTwist's trial fails, shares collapse."),
        ]
        tier, mult, headline = random.choice(events)
        hit = [a for a in self.assets.values() if a.tier == tier]
        if tier == "stock" and "GeneTwist" in headline:
            hit = [a for a in self.assets.values() if a.id == "genetwist"]
        elif tier == "stock":
            hit = [random.choice(hit)] if hit else []
        for a in hit:
            a.price = max(0.01, a.price * mult)
        self.news = headline

    def update(self, dt: float) -> None:
        """Advance the market by real time `dt` (call every frame, any game mode)."""
        self.tick_accum += dt
        while self.tick_accum >= TICK_SECONDS:
            self.tick_accum -= TICK_SECONDS
            self._step_prices()

    def _catch_up(self) -> None:
        """Apply ticks for wall-clock time spent away; stash a summary for the UI."""
        now = time.time()
        if self.last_seen <= 0 or now <= self.last_seen:
            self.last_seen = now
            return
        elapsed = min(now - self.last_seen, OFFLINE_CAP_SECONDS)
        n = int(elapsed // TICK_SECONDS)
        self.last_seen = now
        if n <= 0:
            return
        before = self.net_worth()
        for _ in range(n):
            self._step_prices()
        gained = self.net_worth() - before
        if abs(gained) >= 1.0:
            self.away = {"gained": gained, "hours": elapsed / 3600.0,
                         "value": self.net_worth()}

    # ---- queries ---------------------------------------------------------
    def holdings_value(self) -> float:
        return sum(self.holdings.get(a.id, 0.0) * a.price for a in self.assets.values())

    def net_worth(self) -> float:
        """Everything inside the market: savings + the value of all holdings."""
        return self.savings + self.holdings_value()

    def venue_assets(self, venue: str) -> list:
        return [a for a in self.assets.values() if a.venue == venue]

    # ---- trades (return the cash delta; caller applies it to Game.cash) ---
    def buy(self, asset_id: str, dollars: float) -> float:
        """Buy `dollars` worth of an asset. Returns the cash actually spent."""
        a = self.assets.get(asset_id)
        if a is None or dollars <= 0 or a.price <= 0:
            return 0.0
        self.holdings[asset_id] = self.holdings.get(asset_id, 0.0) + dollars / a.price
        return dollars

    def sell(self, asset_id: str, dollars: float) -> float:
        """Sell up to `dollars` worth. Returns the cash proceeds."""
        a = self.assets.get(asset_id)
        if a is None or a.price <= 0:
            return 0.0
        have = self.holdings.get(asset_id, 0.0)
        shares = min(have, dollars / a.price)
        self.holdings[asset_id] = have - shares
        return shares * a.price

    def sell_all(self, asset_id: str) -> float:
        a = self.assets.get(asset_id)
        if a is None:
            return 0.0
        proceeds = self.holdings.get(asset_id, 0.0) * a.price
        self.holdings[asset_id] = 0.0
        return proceeds

    def deposit_savings(self, amount: float) -> float:
        amount = max(0.0, amount)
        self.savings += amount
        return amount

    def withdraw_savings(self, amount: float) -> float:
        amount = min(self.savings, max(0.0, amount))
        self.savings -= amount
        return amount

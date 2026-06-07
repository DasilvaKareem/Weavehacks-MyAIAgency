"""Customers as an LLM judge → monthly revenue.

Once a month a panel of imagined customers "judges" the company's product (its
name / pitch / customer / business model / pricing) and scores how much the market
wants it, 0-100. That score drives the month's revenue, scaled by team size (more
people = more reach). It's a sandbox, not a real eval harness: the point is that a
better-defined product earns more money, judged by a customer LLM.

Mirrors grants.py: runs off the game thread (CompanyLink.request_customers) and is
graceful — with no API key (or any error) it falls back to a profile-completeness
heuristic so the game still earns offline.
"""
from __future__ import annotations

import json
import os
import re

CUSTOMER_MODEL = os.getenv("COMPANY_AI_CUSTOMER_MODEL", "gemini-2.5-flash")
BASE_MONTHLY = 4_000      # a perfect product (score 100) with no team earns ~this/mo
REVENUE_CAP = 250_000     # sanity ceiling per month

_PROMPT = """You are a panel of five very different potential customers (a thrifty
CFO, a trend-chasing Gen-Z shopper, a busy parent, a skeptical enterprise buyer,
and an early-adopter techie). React to this startup's product honestly — some of
you would buy, some would pass.

Product on file: {company}

Score how much the market overall wants this RIGHT NOW from 0 (nobody buys) to 100
(everyone's lining up). Be a tough but fair crowd: vague, generic, or unpriced
products score low; a specific product with a clear customer and sensible pricing
scores high.

Respond with ONLY a JSON object and nothing else:
{{"score": <integer 0-100>, "buzz": "<one punchy customer quip, <=80 chars>"}}"""


def judge_revenue(company: dict, team: int = 0) -> dict:
    """Judge the product as a customer panel; return
    {score:int 0-100, revenue:int, buzz:str}. Never raises."""
    brief = _company_brief(company)
    if brief is None:                       # no product defined yet → no customers
        return _verdict(0, 0, "No product to sell yet — define your pitch first.")
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        return _heuristic(company, team)
    try:
        from google import genai
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model=CUSTOMER_MODEL, contents=_PROMPT.format(company=brief))
        data = _parse(resp.text or "")
        if data is None:
            return _heuristic(company, team)
        score = max(0, min(100, int(data.get("score", 0) or 0)))
        buzz = str(data.get("buzz") or "").strip()[:80] or "The market shrugged."
        return _verdict(score, _revenue(score, team), buzz)
    except Exception:
        return _heuristic(company, team)


def _revenue(score: int, team: int) -> int:
    """Monthly revenue from the customer score, scaled by headcount (reach)."""
    rev = (score / 100.0) * BASE_MONTHLY * (1.0 + 0.4 * max(0, team))
    return max(0, min(REVENUE_CAP, int(rev)))


def _verdict(score: int, revenue: int, buzz: str) -> dict:
    return {"score": int(score), "revenue": int(revenue), "buzz": buzz}


def _company_brief(company: dict) -> str | None:
    """A one-line product brief, or None if there's basically nothing to sell."""
    if not company:
        return None
    keys = ("company_name", "name", "pitch", "customer", "business_model", "pricing")
    bits = [f"{k.replace('_', ' ')}: {company[k]}" for k in keys if company.get(k)]
    return "; ".join(bits) if bits else None


def _parse(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _heuristic(company: dict, team: int) -> dict:
    """Offline fallback: score on how fully the product is defined (a proxy for how
    sellable it is), so a fleshed-out company still earns without an API key."""
    keys = ("pitch", "customer", "business_model", "pricing", "brand")
    filled = sum(1 for k in keys if (company or {}).get(k))
    score = min(100, 25 + filled * 15)          # 25..100
    quips = ["Word's getting around.", "Steady trickle of buyers.",
             "Customers are curious.", "A few regulars now."]
    return _verdict(score, _revenue(score, team), quips[filled % len(quips)])

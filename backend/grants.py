"""LLM-judged small-business grants.

The player applies at the Grants Office with a written case; an LLM "review board"
judges it and either awards funding (with a dollar amount + a program name) or
declines (with feedback). Runs off the game thread (see CompanyLink.request_grant).

Graceful by design: with no API key (or any error) it falls back to a simple
length/effort heuristic so the game still works offline — same stance as
designer_tools / animator_tools.
"""
from __future__ import annotations

import json
import os
import re

GRANT_MODEL = os.getenv("COMPANY_AI_GRANT_MODEL", "gemini-2.5-flash")
MAX_GRANT = 50_000

_PROMPT = """You are the review board for a competitive small-business innovation grant.
A founder has applied for funding.

Company on file: {company}
Their written application: "{application}"

Judge it fairly but with real standards. Fund it ONLY if the case is specific,
credible, and shows a clear use of funds and impact. Vague, generic, or low-effort
applications must be declined. Scale the amount to quality.

Respond with ONLY a JSON object and nothing else:
{{"approved": true or false, "amount": <integer dollars, 0 if declined else 2000-50000>, "program": "<short grant program name>", "feedback": "<1-2 sentence verdict to the founder>"}}"""


def judge_grant(application: str, company: dict) -> dict:
    """Judge a grant application. Returns
    {approved: bool, amount: int, program: str, feedback: str}. Never raises."""
    application = (application or "").strip()
    if not application:
        return _verdict(False, 0, "Small Business Grant",
                        "You didn't actually make a case. Come back with a real pitch.")
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        return _heuristic(application)
    try:
        from google import genai
        client = genai.Client(api_key=key)
        prompt = _PROMPT.format(company=_company_brief(company),
                                application=application[:1000])
        resp = client.models.generate_content(model=GRANT_MODEL, contents=prompt)
        data = _parse(resp.text or "")
        if data is None:
            return _heuristic(application)
        amt = max(0, min(MAX_GRANT, int(data.get("amount", 0) or 0)))
        approved = bool(data.get("approved")) and amt > 0
        return _verdict(approved, amt if approved else 0,
                        str(data.get("program") or "Small Business Grant")[:40],
                        str(data.get("feedback") or "")[:240])
    except Exception:
        return _heuristic(application)


def _verdict(approved: bool, amount: int, program: str, feedback: str) -> dict:
    return {"approved": approved, "amount": int(amount), "program": program,
            "feedback": feedback}


def _company_brief(company: dict) -> str:
    if not company:
        return "(an early-stage startup; details not yet on file)"
    keys = ("company_name", "name", "pitch", "customer", "business_model", "pricing")
    bits = [f"{k.replace('_', ' ')}: {company[k]}" for k in keys if company.get(k)]
    return "; ".join(bits) or "(an early-stage startup)"


def _parse(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _heuristic(application: str) -> dict:
    """Offline fallback: reward a specific, substantive case (length as a proxy)."""
    words = application.split()
    score = min(1.0, len(words) / 40.0)
    if score >= 0.45:
        return _verdict(True, int(3000 + score * 22000), "Founders Innovation Grant",
                        "Specific and credible — approved. Put it to good use.")
    return _verdict(False, 0, "Founders Innovation Grant",
                    "Too thin to fund. Be specific about what you'd build and its impact.")

"""Composio-backed tools: Google & SaaS app access for role agents.

Composio handles per-user OAuth for hundreds of apps (Gmail, Calendar, Drive,
Sheets, …). A role maps to one or more Composio toolkits in ROLE_PROFILES; this
module loads those as LangChain tools scoped to COMPOSIO_USER_ID. Like the other
bridges it degrades gracefully: no key (or an unreachable backend, or a bad key)
→ [] and prompt-only fallback, so the game always runs.

NOTE: each toolkit a role uses must be CONNECTED for the user in Composio (its
OAuth flow), or tool execution returns a not-connected error. Connect via
`composio link <app>` or the Composio dashboard for the matching user/entity.
"""
from __future__ import annotations

import logging

from . import config

log = logging.getLogger("company.composio")

_client = None
# Cache tools per (toolkits, user) so each role connects at most once.
_cache: dict = {}


def is_configured() -> bool:
    """True when a Composio API key and a user id are both present."""
    return bool(config.COMPOSIO_API_KEY and config.COMPOSIO_USER_ID)


def _get_client():
    global _client
    if _client is None:
        from composio import Composio
        from composio_langchain import LangchainProvider

        _client = Composio(provider=LangchainProvider(), api_key=config.COMPOSIO_API_KEY)
    return _client


def load_composio_tools(toolkits, user_id=None) -> list:
    """LangChain tools for the given Composio toolkits, scoped to a user.

    `toolkits` is a list of slugs (e.g. ['GMAIL','GOOGLECALENDAR']). Returns [] if
    unconfigured, no toolkits, or the backend is unreachable. Cached per
    (toolkits, user). Synchronous — async callers should wrap in a thread.
    """
    if not is_configured() or not toolkits:
        return []
    user = user_id or config.COMPOSIO_USER_ID
    key = (tuple(sorted(toolkits)), user)
    if key in _cache:
        return _cache[key]
    try:
        tools = list(_get_client().tools.get(
            user_id=user, toolkits=list(toolkits), limit=config.COMPOSIO_TOOL_LIMIT))
        log.info("Loaded %d Composio tool(s) for %s: %s", len(tools),
                 ", ".join(toolkits), ", ".join(getattr(t, "name", "?") for t in tools))
    except Exception as exc:  # bad/expired key, network, not-connected — never fatal
        log.warning("Composio unavailable for %s (%s); prompt-only fallback.",
                    toolkits, exc)
        tools = []
    _cache[key] = tools
    return tools


# --- in-game connection management (OAuth) ----------------------------------
# A raw client (no LangchainProvider) for account/auth-config calls.
_raw = None


def _raw_client():
    global _raw
    if _raw is None:
        from composio import Composio
        _raw = Composio(api_key=config.COMPOSIO_API_KEY)
    return _raw


def _norm(tk: str) -> str:
    return (tk or "").strip().lower()


def toolkit_status(toolkits, user_id=None) -> dict:
    """Map each toolkit slug -> 'active' | 'expired' | 'missing' for the user, so
    the game can show whether an agent's apps are connected. [] / {} if keyless."""
    if not is_configured() or not toolkits:
        return {}
    user = user_id or config.COMPOSIO_USER_ID
    try:
        accts = _raw_client().connected_accounts.list()
    except Exception as exc:
        log.warning("Composio status check failed (%s)", exc)
        return {_norm(t): "unknown" for t in toolkits}
    have: dict = {}
    for a in getattr(accts, "items", accts) or []:
        tk = getattr(getattr(a, "toolkit", None), "slug", None)
        if tk:
            # 'active'/'initiated'/'expired'/... ; keep the strongest seen.
            s = str(getattr(a, "status", "")).lower()
            cur = have.get(_norm(tk))
            have[_norm(tk)] = "active" if (s == "active" or cur == "active") else s
    out = {}
    for t in toolkits:
        s = have.get(_norm(t))
        if s is None:
            out[_norm(t)] = "missing"          # never connected
        elif s == "active":
            out[_norm(t)] = "active"
        else:
            out[_norm(t)] = "expired"          # exists but needs re-auth
    return out


def _auth_config_id(c, toolkit: str):
    """Find an existing auth config for the toolkit, else create a managed one."""
    slug = _norm(toolkit)
    try:
        for a in getattr(c.auth_configs.list(), "items", []) or []:
            tk = getattr(getattr(a, "toolkit", None), "slug", None)
            if tk and _norm(tk) == slug:
                return getattr(a, "id", None)
    except Exception:
        pass
    try:
        a = c.auth_configs.create(
            toolkit, options={"type": "use_composio_managed_auth", "name": slug})
        return getattr(a, "id", None)
    except Exception as exc:
        log.warning("Composio auth-config create failed for %s (%s)", toolkit, exc)
        return None


def connect_url(toolkit, user_id=None) -> str | None:
    """A browser URL the CEO opens to (re)authorize `toolkit` for the user, or None.
    Blocking (network) — call off the render thread."""
    if not is_configured():
        return None
    user = user_id or config.COMPOSIO_USER_ID
    c = _raw_client()
    acid = _auth_config_id(c, toolkit)
    if not acid:
        return None
    try:
        r = c.connected_accounts.link(user, acid)
    except Exception as exc:
        log.warning("Composio connect link failed for %s (%s)", toolkit, exc)
        return None
    return (getattr(r, "redirect_url", None) or getattr(r, "redirectUrl", None)
            or getattr(r, "url", None) or (r if isinstance(r, str) else None))


def clear_cache() -> None:
    """Drop cached tools so a freshly-authorized toolkit is picked up next load."""
    _cache.clear()

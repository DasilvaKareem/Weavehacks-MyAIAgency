"""Daytona-backed tools: a real cloud dev sandbox for the Software Engineer agent.

Daytona runs code in a secure, ephemeral REMOTE sandbox. That's the key contrast
with exec_tools.py (local shell, OFF by default because it's dangerous): because
Daytona is isolated and disposable, it's safe to enable simply by providing
DAYTONA_API_KEY — the agent gets to actually write and run code, not just describe
it, without touching your machine.

The sandbox is created lazily on first tool use and reused across calls (so an
engineer can iterate with state). Call shutdown() to delete it.
"""
from __future__ import annotations

import logging

from . import config

log = logging.getLogger("company.daytona")

_client = None
_sandbox = None


def is_configured() -> bool:
    """True when a Daytona API key is present."""
    return bool(config.DAYTONA_API_KEY)


def _get_sandbox():
    """Lazily create (and cache) one Daytona sandbox for this process."""
    global _client, _sandbox
    if _sandbox is not None:
        return _sandbox
    from daytona import Daytona, DaytonaConfig, CreateSandboxFromSnapshotParams

    cfg = {"api_key": config.DAYTONA_API_KEY}
    if config.DAYTONA_API_URL:
        cfg["api_url"] = config.DAYTONA_API_URL
    if config.DAYTONA_TARGET:
        cfg["target"] = config.DAYTONA_TARGET
    _client = Daytona(DaytonaConfig(**cfg))
    # public=True → preview URLs are reachable in a browser without a token.
    params = (CreateSandboxFromSnapshotParams(public=True)
              if config.DAYTONA_PUBLIC_PREVIEW else None)
    _sandbox = _client.create(params)
    log.info("Created Daytona sandbox %s", getattr(_sandbox, "id", "?"))
    return _sandbox


def _fmt(resp) -> str:
    """Format an ExecuteResponse (exit_code, result) into a capped text result."""
    out = (getattr(resp, "result", "") or "").strip()
    limit = config.EXEC_MAX_OUTPUT
    if len(out) > limit:
        out = out[:limit] + f"\n…[truncated {len(out) - limit} chars]"
    return f"(exit {getattr(resp, 'exit_code', '?')})\n{out or '(no output)'}"


def load_daytona_tools(author_id: str | None = None, author_name: str = "") -> list:
    """Return the Daytona sandbox tools as LangChain tools, or [] if unconfigured.

    `author_id`/`author_name` (the engineer) are stamped on the live preview when
    serve_site pins it into the company drive for the CEO to view.
    """
    if not is_configured():
        return []

    from langchain_core.tools import tool

    timeout = int(config.EXEC_TIMEOUT_S)

    @tool
    def run_code(code: str) -> str:
        """Run Python code in your Daytona cloud sandbox and return its output.

        Use this to actually execute and verify your code. State (variables,
        installed packages, files) persists across calls within a session.
        """
        try:
            return _fmt(_get_sandbox().process.code_run(code, timeout=timeout))
        except Exception as exc:
            return f"[daytona error: {exc}]"

    @tool
    def run_command(command: str) -> str:
        """Run a shell command in your Daytona cloud sandbox (e.g. pip install, git, ls)."""
        try:
            return _fmt(_get_sandbox().process.exec(command, timeout=timeout))
        except Exception as exc:
            return f"[daytona error: {exc}]"

    @tool
    def serve_site(command: str, port: int) -> str:
        """Start a long-running web server in the sandbox and return a public URL.

        Use after you've built a site/app. `command` must launch a server bound to
        0.0.0.0:<port> (e.g. 'python3 -m http.server 8080' or 'npm start'). It runs
        in the background; the returned URL is live while the sandbox is up. Give
        this URL to the CEO so they can open the site in their browser.
        """
        try:
            from daytona import SessionExecuteRequest

            sb = _get_sandbox()
            session_id = f"serve-{port}"
            try:
                sb.process.create_session(session_id)
            except Exception:
                pass  # session may already exist from a prior call on this port
            sb.process.execute_session_command(
                session_id, SessionExecuteRequest(command=command, run_async=True))
            link = sb.get_preview_link(port)
            url = getattr(link, "url", None) or str(link)
            token = getattr(link, "token", None)
            # Pin the live preview into the company drive so the CEO can open it
            # from the drive (browser or in-game) — not just from this chat reply.
            if url:
                from .company_fs import register_link
                register_link(url, f"/apps/preview-{port}", author_id, author_name)
            msg = f"Serving on {url}"
            if token:
                msg += (f"\n(if the page asks for auth, send header "
                        f"'x-daytona-preview-token: {token}')")
            msg += " — also pinned to the company drive for the CEO to preview."
            return msg
        except Exception as exc:
            return f"[daytona error: {exc}]"

    return [run_code, run_command, serve_site]


def shutdown() -> None:
    """Delete the sandbox if one was created. Safe to call when none exists."""
    global _client, _sandbox
    if _client is not None and _sandbox is not None:
        try:
            _client.delete(_sandbox)
            log.info("Deleted Daytona sandbox %s", getattr(_sandbox, "id", "?"))
        except Exception as exc:  # pragma: no cover - cleanup is best-effort
            log.warning("Failed to delete Daytona sandbox: %s", exc)
    _client, _sandbox = None, None

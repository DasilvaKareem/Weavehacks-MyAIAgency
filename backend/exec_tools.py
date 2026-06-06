"""Local execution tools for agents that need to *act*, not just describe.

Opsera's MCP tools are AI-EXECUTED: they hand back phased instructions that
expect the caller to run shell commands / read-write files and feed the results
back. These tools give a profiled worker (e.g. DevOps) that ability, so it can
actually drive an architecture scan or a deploy rather than narrating it.

SAFETY — read before enabling:
  * The whole layer is OFF unless COMPANY_AI_ALLOW_EXEC is set.
  * File ops are confined to config.exec_workdir(); shell has a timeout, an
    output cap, and a small catastrophic-command denylist.
  * This is a GUARDRAIL, not a sandbox. `run_shell` can still do real damage
    inside the work dir. Only enable it where you're comfortable letting the
    agent run commands unattended.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import config

# Best-effort backstop against the worst mistakes. NOT a security boundary.
_DENY = [
    r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME|\*)",
    r"\bmkfs\b",
    r":\(\)\s*\{\s*:\s*\|\s*:",          # fork bomb
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\bdd\b.*\bif=",
    r">\s*/dev/sd",
    r"\bchmod\s+-R\s+777\s+/",
    r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh)\b",   # pipe-to-shell
]


def _workdir() -> Path:
    return Path(config.exec_workdir()).resolve()


def _resolve(path: str) -> Path:
    """Resolve a path inside the work dir; raise if it escapes."""
    root = _workdir()
    raw = Path(path)
    p = (raw if raw.is_absolute() else root / raw).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path {path!r} is outside the work dir {root}")
    return p


def _cap(text: str) -> str:
    limit = config.EXEC_MAX_OUTPUT
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def load_exec_tools() -> list:
    """Return shell/file tools as LangChain tools, or [] when exec is disabled."""
    if not config.ALLOW_AGENT_EXEC:
        return []

    from langchain_core.tools import tool

    @tool
    def run_shell(command: str) -> str:
        """Run a shell command in the project work dir and return its output.

        Use this for git, builds, tests, security/architecture scans, deploys,
        and to execute the concrete steps an Opsera tool instructs you to run.
        Output is captured (stdout + stderr) and truncated if very long.
        """
        for pat in _DENY:
            if re.search(pat, command):
                return "[refused: command matches a blocked destructive pattern]"
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(_workdir()),
                capture_output=True, text=True, timeout=config.EXEC_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return f"[timed out after {config.EXEC_TIMEOUT_S:.0f}s]"
        except Exception as exc:  # pragma: no cover - defensive
            return f"[error launching command: {exc}]"
        body = proc.stdout or ""
        if proc.stderr:
            body += ("\n[stderr]\n" + proc.stderr)
        return _cap(f"(exit {proc.returncode})\n{body.strip() or '(no output)'}")

    @tool
    def read_file(path: str) -> str:
        """Read a UTF-8 text file inside the work dir."""
        try:
            return _cap(_resolve(path).read_text(errors="replace"))
        except Exception as exc:
            return f"[error: {exc}]"

    @tool
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a UTF-8 text file inside the work dir."""
        try:
            p = _resolve(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"wrote {len(content)} chars to {p.relative_to(_workdir())}"
        except Exception as exc:
            return f"[error: {exc}]"

    @tool
    def list_files(pattern: str = "**/*") -> str:
        """List files in the work dir matching a glob (default: everything)."""
        try:
            root = _workdir()
            hits = [str(p.relative_to(root)) for p in sorted(root.glob(pattern))
                    if p.is_file()]
        except Exception as exc:
            return f"[error: {exc}]"
        shown = hits[:500]
        suffix = "" if len(hits) <= 500 else f"\n…[{len(hits) - 500} more]"
        return ("\n".join(shown) or "(no matches)") + suffix

    return [run_shell, read_file, write_file, list_files]

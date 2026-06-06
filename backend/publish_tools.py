"""Website publishing tool: deploy an agent-built site to Vercel for a real URL.

The Software Engineer / Blogger build a static site in their Daytona cloud
sandbox (daytona_tools.py). serve_site() only gives a preview that dies with the
session; publish_site() ships the same files to Vercel via the Composio Vercel
toolkit, returning a PERMANENT public URL.

Flow: list the built files in the sandbox, download their bytes, and hand them to
Composio's VERCEL_CREATE_A_NEW_DEPLOYMENT action as inline files (one call — the
Vercel API accepts inlined file contents). Degrades gracefully to a clear message
when Composio/Vercel isn't configured, so the game never breaks.

Auth rides on Composio: COMPOSIO_API_KEY + COMPOSIO_USER_ID must be set and a
Vercel account connected for that user (`composio link vercel`).
"""
from __future__ import annotations

import base64
import json
import logging
import re

from . import config, composio_tools, daytona_tools

log = logging.getLogger("company.publish")

# Directories never worth shipping to a static host.
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".next", ".vercel", ".cache"}


def _slug(name: str) -> str:
    """A valid Vercel project name: lowercase, alphanumeric + hyphens."""
    s = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")
    return (s or "company-ai-site")[:90]


def _list_site_files(sandbox, site_dir: str) -> list[str]:
    """Absolute paths of every file under site_dir in the sandbox (via os.walk).

    The listing (paths only) is tiny, so it's safe to read off stdout; the file
    *bytes* are pulled with the FS download API to avoid any output truncation.
    """
    snippet = (
        "import os,json\n"
        f"root=os.path.abspath({site_dir!r})\n"
        f"skip={sorted(_SKIP_DIRS)!r}\n"
        "out=[]\n"
        "for dp,dn,fn in os.walk(root):\n"
        "    dn[:]=[d for d in dn if d not in skip]\n"
        "    for f in fn:\n"
        "        out.append(os.path.join(dp,f))\n"
        "print('<<<P>>>'+json.dumps({'root':root,'files':out})+'<<<E>>>')\n"
    )
    resp = sandbox.process.code_run(snippet, timeout=int(config.EXEC_TIMEOUT_S))
    raw = getattr(resp, "result", "") or ""
    m = re.search(r"<<<P>>>(.*?)<<<E>>>", raw, re.S)
    if not m:
        raise RuntimeError(f"could not list site files: {raw[:400]}")
    data = json.loads(m.group(1))
    return data["root"], data["files"]


def _extract_url(data) -> str | None:
    """Pull a deployment URL out of Vercel's (possibly nested) response."""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "alias" and isinstance(v, list):
                    found.extend(a for a in v if isinstance(a, str))
                elif k == "url" and isinstance(v, str):
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)

    walk(data)
    hosts = [h for h in found if "." in h]
    if not hosts:
        return None
    # Prefer a stable production alias (shortest host) over the per-deploy URL.
    best = min(hosts, key=len)
    return best if best.startswith("http") else f"https://{best}"


def load_publish_tools(author_id: str | None = None, author_name: str = "") -> list:
    """Return the website-publishing tools, or [] if Daytona isn't configured.

    `author_id`/`author_name` (the publishing agent) are stamped on the deployed
    site when it's pinned into the company drive as a live preview.

    (Composio config is checked at call time so the tool can give the agent a
    precise 'connect Vercel' message rather than silently vanishing.)
    """
    if not daytona_tools.is_configured():
        return []

    from langchain_core.tools import tool

    @tool
    def publish_site(site_dir: str = ".", project_name: str = "") -> str:
        """Deploy the website you built in the sandbox to Vercel and return a
        PERMANENT public URL the CEO can open and share.

        Call this after building + verifying the site (e.g. index.html exists).
        `site_dir` is the sandbox folder containing the site (default: current
        dir). `project_name` names the Vercel project (reused on redeploys so the
        URL stays stable); defaults to the company site. Use this for anything
        meant to last; use serve_site only for a throwaway session preview.
        """
        if not composio_tools.is_configured():
            return ("[publish unavailable: set COMPOSIO_API_KEY and COMPOSIO_USER_ID, "
                    "and connect Vercel (`composio link vercel`) for that user.]")
        try:
            sandbox = daytona_tools._get_sandbox()
            root, paths = _list_site_files(sandbox, site_dir)
            if not paths:
                return (f"[publish error: no files found in '{site_dir}'. Build the "
                        "site in the sandbox first, then publish.]")

            files, total = [], 0
            for p in paths:
                data = sandbox.fs.download_file(p)
                if not data:
                    continue
                total += len(data)
                if total > config.WEBSITE_MAX_BYTES:
                    return (f"[publish error: site exceeds {config.WEBSITE_MAX_BYTES} "
                            f"bytes after {len(files)} files — trim assets and retry.]")
                rel = p[len(root):].lstrip("/") or p.rsplit("/", 1)[-1]
                files.append({"file": rel,
                              "data": base64.b64encode(data).decode(),
                              "encoding": "base64"})
            if not files:
                return "[publish error: site files were empty/unreadable.]"

            name = _slug(project_name)
            resp = composio_tools._get_client().tools.execute(
                config.VERCEL_DEPLOY_ACTION,
                arguments={
                    "name": name,
                    "project": name,
                    "target": config.VERCEL_TARGET,
                    "files": files,
                    "projectSettings": {"framework": None},
                },
                user_id=config.COMPOSIO_USER_ID,
            )
            if not resp.get("successful"):
                return (f"[publish failed: {resp.get('error') or 'unknown error'}. "
                        "Is Vercel connected for this Composio user?]")
            url = _extract_url(resp.get("data"))
            if url:
                # Pin the permanent deploy into the company drive as a live preview.
                from .company_fs import register_link
                register_link(url, f"/apps/{name}", author_id, author_name)
                return (f"Published {len(files)} file(s) to Vercel — live at {url} "
                        f"(project '{name}'). Pinned to the company drive too; "
                        f"share this link, it stays up.")
            return ("Deployment created, but no URL was returned. Raw response: "
                    f"{json.dumps(resp.get('data'))[:600]}")
        except Exception as exc:  # never fatal — surface it to the agent/CEO
            log.warning("publish_site failed: %s", exc)
            return f"[publish error: {exc}]"

    return [publish_site]

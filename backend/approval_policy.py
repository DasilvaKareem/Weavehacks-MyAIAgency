"""Trust-tier enforcement for tools used by always-on autonomous runs."""
from __future__ import annotations

import hashlib
import json

from .store import AgentStore

_READ_WORDS = {
    "get", "list", "read", "search", "find", "fetch", "scrape", "crawl",
    "review", "report", "status", "inspect", "query", "lookup",
}
_CRITICAL_WORDS = {
    "delete", "remove", "rm", "fire", "send", "email", "message", "post",
    "publish_post", "write_file", "run_shell",
}
_EXTERNAL_WRITE_WORDS = {
    "create", "update", "edit", "cancel", "deploy", "publish", "write", "add",
}
_SANDBOX_TOOLS = {
    "run_code", "run_command", "serve_site", "generate_image", "generate_video",
    "add_blog_image",
}
_DRIVE_WRITES = {"drive_write"}
_CRITICAL_TOOLS = {"drive_delete", "fire_agent", "run_shell", "write_file"}

_MIN_TIER = {
    "read": "supervised",
    "drive_write": "supervised",
    "sandbox": "standard",
    "external_write": "trusted",
    "critical": None,
}
_TIER_SCORE = {"supervised": 0, "standard": 1, "trusted": 2}


class ApprovalRequired(RuntimeError):
    def __init__(self, tool_name: str, args: dict, fingerprint: str,
                 action_class: str) -> None:
        self.tool_name = tool_name
        self.args = args
        self.fingerprint = fingerprint
        self.action_class = action_class
        super().__init__(f"approval required for {tool_name}")


def canonical_args(args: dict) -> str:
    return json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(tool_name: str, args: dict) -> str:
    raw = f"{tool_name}:{canonical_args(args)}".encode()
    return hashlib.sha256(raw).hexdigest()


def classify(tool_name: str) -> str:
    name = (tool_name or "").lower()
    words = set(name.replace("-", "_").split("_"))
    if name in _CRITICAL_TOOLS or words & _CRITICAL_WORDS:
        return "critical"
    if name in _DRIVE_WRITES:
        return "drive_write"
    if name.startswith("drive_") or words & _READ_WORDS:
        return "read"
    if name in _SANDBOX_TOOLS:
        return "sandbox"
    if words & _EXTERNAL_WRITE_WORDS:
        return "external_write"
    # Unknown dynamic MCP/Composio actions are conservative by default.
    return "critical"


def requires_approval(trust_tier: str, action_class: str) -> bool:
    minimum = _MIN_TIER[action_class]
    if minimum is None:
        return True
    return _TIER_SCORE.get(trust_tier, 0) < _TIER_SCORE[minimum]


def wrap_tools(tools: list, store: AgentStore, run_id: str,
               trust_tier: str) -> list:
    """Wrap LangChain tools so guarded calls pause before side effects."""
    from langchain_core.tools import StructuredTool

    out = []
    for original in tools:
        name = original.name
        action_class = classify(name)

        async def guarded(_original=original, _name=name,
                          _action_class=action_class, **kwargs):
            mark = fingerprint(_name, kwargs)
            if requires_approval(trust_tier, _action_class):
                if not store.consume_grant(run_id, mark):
                    raise ApprovalRequired(_name, kwargs, mark, _action_class)
            return await _original.ainvoke(kwargs)

        out.append(StructuredTool.from_function(
            coroutine=guarded,
            name=name,
            description=original.description,
            args_schema=getattr(original, "args_schema", None),
        ))
    return out

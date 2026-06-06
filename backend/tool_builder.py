"""Build the same role toolset for chat, LangGraph workers, and scheduled runs."""
from __future__ import annotations

import asyncio

from . import config
from .animator_tools import load_animator_tools
from .blogger_tools import load_blogger_tools
from .company_fs import load_fs_tools
from .composio_tools import load_composio_tools
from .daytona_tools import load_daytona_tools
from .designer_tools import load_designer_tools
from .exec_tools import load_exec_tools
from .hr_tools import load_hr_tools
from .mcp_bridge import load_tools, load_tools_sync
from .publish_tools import load_publish_tools


def _local_tools(role: str, agent_id: str | None, agent_name: str) -> list:
    tools = load_fs_tools(author_id=agent_id, author_name=agent_name)
    if not config.role_profile(role):
        return tools
    tools += load_exec_tools()
    if config.role_uses_daytona(role):
        tools += load_daytona_tools(agent_id, agent_name)
    if config.role_uses_image_gen(role):
        tools += load_designer_tools(agent_id, agent_name)
    if config.role_uses_video_gen(role):
        tools += load_animator_tools(agent_id, agent_name)
    if config.role_uses_hr(role):
        tools += load_hr_tools()
    if config.role_uses_blogger(role):
        tools += load_blogger_tools(agent_id, agent_name)
    if config.role_uses_vercel(role):
        tools += load_publish_tools(agent_id, agent_name)
    return tools


def build_tools_sync(role: str, agent_id: str | None, agent_name: str) -> list:
    tools = _local_tools(role, agent_id, agent_name)
    if not config.role_profile(role):
        return tools
    tools += load_tools_sync(config.role_servers(role),
                             apify_actors=config.role_actors(role))
    toolkits = config.role_toolkits(role)
    if toolkits:
        tools += load_composio_tools(toolkits)
    return tools


async def build_tools(role: str, agent_id: str | None, agent_name: str) -> list:
    tools = _local_tools(role, agent_id, agent_name)
    if not config.role_profile(role):
        return tools
    tools += await load_tools(config.role_servers(role),
                              apify_actors=config.role_actors(role))
    toolkits = config.role_toolkits(role)
    if toolkits:
        tools += await asyncio.to_thread(load_composio_tools, toolkits)
    return tools

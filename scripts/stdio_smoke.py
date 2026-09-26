"""Exercise the unchanged default stdio transport without Yoto API requests."""
from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def smoke() -> None:
    env = dict(os.environ)
    env.update(YOTO_ALLOW_WRITES="0", YOTO_CLIENT_ID="ci-public-client")
    params = StdioServerParameters(command=sys.executable, args=["-m", "yoto_mcp"], env=env)
    async with (
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        hello = await session.initialize()
        tools = await session.list_tools()
        tool_names = {item.name for item in tools.tools}
        if hello.server_info.name != "yoto-mcp" or not {
            "add_youtube", "remove_empty_chapter",
        } <= tool_names:
            raise RuntimeError("Default stdio MCP handshake returned unexpected tools")
        print(f"Stdio smoke passed: {len(tools.tools)} tools, {hello.protocol_version}")


if __name__ == "__main__":
    asyncio.run(smoke())

"""Real stdio MCP handshake and subprocess protocol framing, without API traffic."""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_roundtrip_discovery_schema_and_resource() -> None:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "flighty_mcp"], env={"FLIGHTY_ALLOW_WRITES": "false"}
    )
    async with (
        asyncio.timeout(20),
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        assert initialized.server_info.name == "flighty"
        tools = await session.list_tools()
        assert len(tools.tools) == 55
        result = await session.call_tool("get_schema", {"name": "SyncRequestProto"})
        assert not result.is_error
        assert result.structured_content["name"] == "SyncRequestProto"
        resource = await session.read_resource("flighty://schema/SyncRequestProto")
        assert "SyncRequestProto" in resource.contents[0].text

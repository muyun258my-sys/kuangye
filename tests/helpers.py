"""测试辅助：内存直连的 MCP ClientSession + 工具调用解包。"""
import json
from contextlib import asynccontextmanager


@asynccontextmanager
async def mcp_session(server):
    """走完整 MCP 协议（initialize / list_tools / call_tool），但不起子进程。"""
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server) as client:
        yield client


async def call(client, tool: str, args: dict | None = None):
    """调用工具，返回 (is_error, data)。data 优先取 structuredContent，否则解析文本。"""
    res = await client.call_tool(tool, args or {})
    if res.isError:
        return True, " ".join(getattr(c, "text", "") for c in res.content)
    if res.structuredContent is not None:
        return False, res.structuredContent
    return False, json.loads(res.content[0].text)

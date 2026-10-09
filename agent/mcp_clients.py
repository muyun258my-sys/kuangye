"""3 个 MCP server 的连接管理。

MCP_TRANSPORT=stdio（默认）  以子进程方式拉起 3 个 server（和 Claude Desktop 一样）
MCP_TRANSPORT=http           连接已在运行的 streamable-http server（docker-compose 用）
                              地址：NEWS_MCP_URL / PDF_MCP_URL / PRICE_MCP_URL
memory（仅测试）             进程内直连，不起子进程
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import anyio
import httpx

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

log = logging.getLogger("agent.mcp")
ROOT = Path(__file__).resolve().parent.parent

SERVERS = {  # 逻辑名 -> (python 模块, 默认端口, URL 环境变量)
    "news": ("servers.mining_news.server", 8001, "NEWS_MCP_URL"),
    "pdf": ("servers.mineral_pdf.server", 8002, "PDF_MCP_URL"),
    "price": ("servers.lme_price.server", 8003, "PRICE_MCP_URL"),
}


class ToolCallError(RuntimeError):
    def __init__(self, server: str, tool: str, message: str):
        super().__init__(f"{server}.{tool}: {message}")
        self.server, self.tool, self.message = server, tool, message


class MCPHub:
    """持有 3 个 ClientSession，统一 call(server, tool, args) -> dict。"""

    def __init__(self, sessions: dict[str, ClientSession], transport: str):
        self.sessions = sessions
        self.transport = transport
        self.calls: list[dict] = []  # 调用日志，便于调试 / 测试断言

    async def list_tools(self) -> dict[str, list[str]]:
        return {k: [t.name for t in (await s.list_tools()).tools] for k, s in self.sessions.items()}

    async def call(self, server: str, tool: str, args: dict[str, Any] | None = None) -> dict:
        args = args or {}
        res = await self.sessions[server].call_tool(tool, args)
        text = " ".join(getattr(c, "text", "") for c in res.content)
        self.calls.append({"server": server, "tool": tool, "args": args, "isError": bool(res.isError)})
        if res.isError:
            raise ToolCallError(server, tool, text or "unknown error")
        if res.structuredContent is not None:
            data = res.structuredContent
            # FastMCP 对非 dict 返回值会包一层 {"result": ...}
            return data["result"] if set(data) == {"result"} else data
        return json.loads(text)


def _stdio_params(module: str) -> StdioServerParameters:
    env = {**os.environ, "MCP_TRANSPORT": "stdio", "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    return StdioServerParameters(command=sys.executable, args=["-m", module], cwd=str(ROOT), env=env)


def _mcp_http_client() -> httpx.AsyncClient:
    """agent → 自己的 MCP server 不走代理。

    httpx 默认读代理环境变量，Windows 上还会读注册表里的系统代理（Clash / V2Ray 等），
    却不认系统代理的绕过列表，结果连 127.0.0.1:8003 也被转发到代理，返回 502。
    确实需要经代理访问远程 MCP server 时设置 MCP_HTTP_USE_PROXY=1。
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30, read=300),
        trust_env=os.environ.get("MCP_HTTP_USE_PROXY", "") == "1",
    )


async def _wait_port(url: str, retries: int) -> None:
    """docker-compose 里 server 可能比 agent 晚就绪：先等端口可连，再建 MCP 会话。"""
    u = urlparse(url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    last: Exception | None = None
    for i in range(retries):
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 3)
            w.close()
            await w.wait_closed()
            return
        except (OSError, asyncio.TimeoutError) as e:
            last = e
            log.info("等待 %s 就绪（%d/%d）：%s", url, i + 1, retries, e)
            await asyncio.sleep(min(2**i, 5))
    raise ConnectionError(f"无法连接 MCP server {url}：{last}（server 启动了吗？地址/端口对吗？）")


async def _open_http(stack: AsyncExitStack, url: str, retries: int) -> ClientSession:
    await _wait_port(url, retries)
    client = await stack.enter_async_context(_mcp_http_client())
    r, w, _ = await stack.enter_async_context(streamable_http_client(url, http_client=client))
    s = await stack.enter_async_context(ClientSession(r, w))
    with anyio.fail_after(30):  # 不用 asyncio.wait_for：它会换 task 执行，和 anyio 的 cancel scope 冲突
        await s.initialize()
    return s


@asynccontextmanager
async def connect(transport: str | None = None, http_retries: int = 8):
    transport = (transport or os.environ.get("MCP_TRANSPORT", "stdio")).lower()
    if transport in {"streamable-http", "streamable_http"}:
        transport = "http"
    async with AsyncExitStack() as stack:
        sessions: dict[str, ClientSession] = {}
        if transport == "memory":
            import importlib

            from mcp.shared.memory import create_connected_server_and_client_session

            for name, (module, _, _) in SERVERS.items():
                srv = importlib.import_module(module).mcp
                sessions[name] = await stack.enter_async_context(create_connected_server_and_client_session(srv))
        elif transport == "http":
            for name, (_, port, env) in SERVERS.items():
                url = os.environ.get(env, f"http://127.0.0.1:{port}/mcp")
                sessions[name] = await _open_http(stack, url, http_retries)
        elif transport == "stdio":
            errlog = open(os.devnull, "w") if os.environ.get("AGENT_QUIET_SERVERS", "1") == "1" else sys.stderr  # noqa: SIM115
            stack.callback(lambda: errlog is not sys.stderr and errlog.close())
            for name, (module, _, _) in SERVERS.items():
                r, w = await stack.enter_async_context(stdio_client(_stdio_params(module), errlog=errlog))
                s = await stack.enter_async_context(ClientSession(r, w))
                await s.initialize()
                sessions[name] = s
        else:
            raise ValueError(f"未知 MCP_TRANSPORT={transport}（可选 stdio / http）")
        log.info("已连接 MCP servers（%s）：%s", transport, ", ".join(sessions))
        yield MCPHub(sessions, transport)

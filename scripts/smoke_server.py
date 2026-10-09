"""冒烟测试：像 Claude Desktop 一样以子进程 (stdio) 或 HTTP 方式连接某个 server，列出工具并调用一个。

用法：
  uv run python scripts/smoke_server.py lme_price                      # stdio
  uv run python scripts/smoke_server.py lme_price --http http://127.0.0.1:8003/mcp
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent

SAMPLE_CALLS = {
    "lme_price": ("get_trend", {"commodity": "lithium", "days": 7}),
    "mining_news": ("search", {"query": "Pilbara lithium", "days": 7}),
    "mineral_pdf": ("extract_resources", {"pdf_url": "fixture://pilgangoora"}),
}


async def _run(session: ClientSession, server: str) -> None:
    await session.initialize()
    tools = (await session.list_tools()).tools
    print("tools:", [t.name for t in tools])
    name, args = SAMPLE_CALLS[server]
    res = await session.call_tool(name, args)
    text = res.content[0].text if res.content else ""
    print(f"{name}({args}) isError={res.isError}")
    print(json.dumps(json.loads(text), ensure_ascii=False)[:400] if text.startswith(("{", "[")) else text[:400])
    if res.isError:
        sys.exit(1)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("server", choices=sorted(SAMPLE_CALLS))
    ap.add_argument("--http", help="streamable-http 地址，如 http://127.0.0.1:8003/mcp")
    a = ap.parse_args()
    if a.http:
        async with httpx.AsyncClient(trust_env=False) as hc, streamable_http_client(a.http, http_client=hc) as (r, w, _):
            async with ClientSession(r, w) as s:
                await _run(s, a.server)
    else:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", f"servers.{a.server}.server"],
            cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT), "MCP_TRANSPORT": "stdio"},
        )
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await _run(s, a.server)


if __name__ == "__main__":
    asyncio.run(main())

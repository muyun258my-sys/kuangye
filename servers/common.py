"""3 个 MCP server 共用的小工具：时间戳、fixtures 路径、离线开关、HTTP 客户端、传输方式切换。"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(os.environ.get("MINING_FIXTURES_DIR", ROOT / "fixtures"))

USER_AGENT = "mining-briefing/0.1 (+https://github.com/; research demo)"
HTTP_TIMEOUT = float(os.environ.get("MINING_HTTP_TIMEOUT", "8"))

# stdio 模式下 stdout 是协议通道，日志只能写 stderr
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    stream=sys.stderr,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_offline() -> bool:
    """MINING_OFFLINE=1 时完全不发网络请求，直接走 fixtures / mock。"""
    return os.environ.get("MINING_OFFLINE", "").lower() in {"1", "true", "yes"}


def http_client(**kw) -> httpx.Client:
    return httpx.Client(
        timeout=kw.pop("timeout", HTTP_TIMEOUT),
        headers={"User-Agent": USER_AGENT, **kw.pop("headers", {})},
        follow_redirects=True,
        **kw,
    )


def threaded(fn):
    """把阻塞的同步工具函数放到工作线程里执行。

    FastMCP 对同步工具是在事件循环里直接调用的：一个工具在等网络时，同一 server 的
    其它请求全部被卡住（agent 并发发 4 个 search 实际会变成串行）。包一层 async + to_thread
    后请求才能真正并发。functools.wraps 保留签名和 docstring，FastMCP 生成的 schema 不变。
    """
    import functools

    import anyio

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return wrapper


def make_server(name: str, default_port: int, instructions: str = ""):
    """创建 FastMCP 实例。

    host/port 必须在构造时传入：FastMCP 会根据构造时的 host 决定是否开启
    DNS rebinding 保护（127.0.0.1 时只放行 localhost）。docker 里用 MCP_HOST=0.0.0.0，
    这样其它容器用服务名（如 http://price:8003/mcp）访问时不会被 421 拒绝。
    """
    from mcp.server.fastmcp import FastMCP

    return FastMCP(
        name,
        instructions=instructions or None,
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", default_port)),
        stateless_http=True,
    )


def run_server(mcp) -> None:
    """同一份代码：MCP_TRANSPORT=stdio（默认）或 streamable-http（端点 /mcp）。"""
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if transport in {"http", "streamable-http", "streamable_http"}:
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")

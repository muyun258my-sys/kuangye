"""阶段 0 环境检查：确认 Python 版本、全部依赖可导入、关键 MCP SDK 接口存在。

运行：uv run python scripts/check_env.py
"""
import importlib
import sys
from pathlib import Path

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 让 scripts/ 下也能 import servers.*

ok = True


def check(label, fn):
    global ok
    try:
        detail = fn()
        print(f"[PASS] {label}" + (f"  ({detail})" if detail else ""))
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"[FAIL] {label}: {type(e).__name__}: {e}")


def _py():
    if sys.version_info < (3, 11):
        raise RuntimeError(f"当前 {sys.version.split()[0]}，需要 3.11+")
    return sys.version.split()[0]


check("Python >= 3.11", _py)

for mod in ["mcp", "httpx", "feedparser", "trafilatura", "pdfplumber", "langgraph", "openai", "dotenv", "pytest", "reportlab"]:
    check(f"import {mod}", lambda m=mod: getattr(importlib.import_module(m), "__version__", ""))


def _mcp_api():
    from importlib.metadata import version

    from mcp import ClientSession, StdioServerParameters  # noqa: F401
    from mcp.client.stdio import stdio_client  # noqa: F401
    from mcp.client.streamable_http import streamable_http_client  # noqa: F401
    from mcp.server.fastmcp import FastMCP  # noqa: F401
    from mcp.server.fastmcp.exceptions import ToolError  # noqa: F401
    from mcp.shared.memory import create_connected_server_and_client_session  # noqa: F401
    return "mcp " + version("mcp")


check("MCP SDK 接口 (FastMCP / ClientSession / stdio / streamable-http / memory)", _mcp_api)


def _lg_api():
    from importlib.metadata import version

    from langgraph.graph import END, START, StateGraph  # noqa: F401
    return "langgraph " + version("langgraph")


check("LangGraph 接口 (StateGraph)", _lg_api)


def _common():
    from servers.common import FIXTURES, now_iso
    return f"now={now_iso()} fixtures={FIXTURES}"


check("servers.common 可导入", _common)

print("\n阶段 0 检查：" + ("全部通过 ✅" if ok else "存在失败 ❌"))
sys.exit(0 if ok else 1)

"""命令行入口。

  uv run python -m agent.cli "给我生成一份关于 Pilbara 锂矿的今日简报"
  uv run python -m agent.cli --offline "..."          # 断网 + 不用 LLM
  uv run python -m agent.cli --transport http "..."   # 连接已运行的 HTTP server

报告打印到 stdout，同时保存到 reports/briefing-<日期>.md（--out 可改）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUERY = "给我生成一份关于 Pilbara 锂矿的今日简报"


def _utf8_stdio() -> None:
    # Windows 控制台默认 GBK，中文 Markdown 会报 UnicodeEncodeError
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


async def amain(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mining-briefing", description="矿权日报 Agent")
    ap.add_argument("query", nargs="*", help=f"需求描述，默认：{DEFAULT_QUERY}")
    ap.add_argument("--offline", action="store_true", help="不联网、不调用 LLM，全部使用 fixtures / mock")
    ap.add_argument("--no-llm", action="store_true", help="联网取数，但用离线模板写报告")
    ap.add_argument("--transport", choices=["stdio", "http"], help="MCP 连接方式（默认读 MCP_TRANSPORT，否则 stdio）")
    ap.add_argument("--out", help="报告保存路径，默认 reports/briefing-<日期>.md")
    ap.add_argument("--json", help="另存完整 state（调试用）")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    _utf8_stdio()
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    if a.offline:
        os.environ["MINING_OFFLINE"] = "1"  # stdio 模式下子进程 server 会继承
        if (a.transport or os.environ.get("MCP_TRANSPORT", "stdio")).lower() in {"http", "streamable-http"}:
            print("[warn] HTTP 模式下 server 是独立进程，--offline 只关闭 LLM；"
                  "要让 server 也离线，请在启动 server 时设置 MINING_OFFLINE=1", file=sys.stderr)

    from agent.graph import run_briefing
    from agent.llm import LLM
    from agent.mcp_clients import connect

    llm = LLM.from_env(disabled=a.offline or a.no_llm)
    query = " ".join(a.query).strip() or DEFAULT_QUERY
    async with connect(a.transport) as hub:
        state = await run_briefing(query, hub, llm)

    md = state["report_md"]
    out = Path(a.out) if a.out else ROOT / "reports" / f"briefing-{state['plan']['date']}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    if a.json:
        Path(a.json).write_text(json.dumps(state, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(md)
    print(f"\n[已保存] {out}", file=sys.stderr)
    for e in state.get("errors", []):
        print(f"[warn] {e}", file=sys.stderr)
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()

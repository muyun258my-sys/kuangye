"""lme-price-mcp：金属 / 锂价格行情 MCP server。

工具：
  get_price(commodity, date)      某日（或之前最近一个交易日）的收盘价
  get_trend(commodity, days=30)   价格序列 + 涨跌幅 / 波动率 / 均线
  list_commodities()              支持的品种

运行：python -m servers.lme_price.server   （MCP_TRANSPORT=streamable-http 切到 HTTP，默认端口 8003）
"""
from __future__ import annotations

import math
import statistics
from datetime import date as Date, timedelta
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

from servers.common import make_server, now_iso, run_server, threaded
from servers.lme_price.providers import COMMODITIES, ProviderError, fetch_history, resolve_commodity

mcp = make_server(
    "lme-price",
    default_port=8003,
    instructions="金属与锂价格行情。每个结果都带 source（yahoo / metals-api / mock）和 source_url；source=mock 表示模拟数据。",
)


def _commodity(name: str):
    try:
        return resolve_commodity(name)
    except KeyError as e:
        raise ToolError(str(e.args[0])) from e


def _parse_date(s: str) -> Date:
    s = (s or "").strip().lower()
    if s in {"", "today", "latest", "今天", "今日"}:
        return Date.today()
    try:
        return Date.fromisoformat(s)
    except ValueError as e:
        raise ToolError(f"日期格式错误 '{s}'，应为 YYYY-MM-DD 或 'today'") from e


def _pct(a: float, b: float) -> float | None:
    return round((b - a) / a * 100, 2) if a else None


@mcp.tool()
def list_commodities() -> dict[str, Any]:
    """列出支持的品种及其单位、上游代码。"""
    return {
        "commodities": [
            {"key": c.key, "name": c.name, "unit": f"{c.currency}/{c.unit}", "yahoo": c.yahoo, "metals_api": c.metals_api}
            for c in COMMODITIES.values()
        ],
        "retrieved_at": now_iso(),
    }


@mcp.tool()
@threaded
def get_price(commodity: str, date: str = "today") -> dict[str, Any]:
    """查询某品种在某日的收盘价；非交易日返回此前最近一个交易日（见 as_of）。

    Args:
        commodity: 品种，如 lithium / spodumene / copper / aluminium / nickel（支持中文：锂、铜…）
        date: YYYY-MM-DD，或 'today'
    """
    c = _commodity(commodity)
    d = _parse_date(date)
    if d > Date.today():
        raise ToolError(f"日期 {d} 在未来")
    try:
        s = fetch_history(c, d - timedelta(days=14), d)
    except ProviderError as e:
        raise ToolError(str(e)) from e
    as_of, price = s.points[-1]
    return {
        "commodity": c.key,
        "name": c.name,
        "date": d.isoformat(),
        "as_of": as_of.isoformat(),
        "price": price,
        "currency": c.currency,
        "unit": c.unit,
        "source": s.source,
        "data_source": s.data_source,
        "source_url": s.source_url,
        "retrieved_at": now_iso(),
        "note": s.note,
    }


@mcp.tool()
@threaded
def get_trend(commodity: str, days: int = 30) -> dict[str, Any]:
    """最近 N 个自然日的价格序列与统计：涨跌幅、最高/最低、年化波动率、7/30 日均线。

    Args:
        commodity: 品种，如 lithium / copper
        days: 回看自然日天数，2–365，默认 30
    """
    c = _commodity(commodity)
    if not 2 <= int(days) <= 365:
        raise ToolError("days 必须在 2–365 之间")
    end = Date.today()
    start = end - timedelta(days=int(days))
    # 多取 14 天：用"区间起点当天或之前最近的收盘价"作为基准，
    # 这样 7 天涨跌 = 最新价 vs 7 天前的价格；成交稀疏的品种（如 LTH=F）也能算
    try:
        s = fetch_history(c, start - timedelta(days=14), end, min_points=2)
    except ProviderError as e:
        raise ToolError(str(e)) from e
    before = [p for p in s.points if p[0] <= start]
    pts = before[-1:] + [p for p in s.points if p[0] > start]
    if len(pts) < 2:
        raise ToolError(f"区间内数据点不足（{len(pts)} 个）")
    closes = [v for _, v in pts]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    vol = statistics.stdev(rets) * math.sqrt(252) * 100 if len(rets) >= 2 else None

    def ma(n: int) -> float | None:
        return round(sum(closes[-n:]) / n, 4) if len(closes) >= n else None

    return {
        "commodity": c.key,
        "name": c.name,
        "currency": c.currency,
        "unit": c.unit,
        "days": int(days),
        "window_start": start.isoformat(),
        "start_date": pts[0][0].isoformat(),
        "end_date": pts[-1][0].isoformat(),
        "series": [{"date": d.isoformat(), "close": v} for d, v in pts],
        "stats": {
            "first": closes[0],
            "last": closes[-1],
            "change_abs": round(closes[-1] - closes[0], 4),
            "change_pct": _pct(closes[0], closes[-1]),
            "high": max(closes),
            "low": min(closes),
            "volatility_annualized_pct": round(vol, 2) if vol is not None else None,
            "ma7": ma(5),  # 7 个自然日 ≈ 5 个交易日
            "ma30": ma(21),  # 30 个自然日 ≈ 21 个交易日
            "points": len(closes),
        },
        "source": s.source,
        "data_source": s.data_source,
        "source_url": s.source_url,
        "retrieved_at": now_iso(),
        "note": s.note,
    }


def main() -> None:
    run_server(mcp)


if __name__ == "__main__":
    main()

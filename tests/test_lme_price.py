from datetime import date, timedelta

import httpx
import pytest

from helpers import call, mcp_session
from servers.lme_price import providers
from servers.lme_price.server import mcp


async def test_list_tools():
    async with mcp_session(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert {"get_price", "get_trend", "list_commodities"} <= names


async def test_get_price_offline_mock_is_labelled_and_deterministic():
    async with mcp_session(mcp) as c:
        err, a = await call(c, "get_price", {"commodity": "lithium", "date": "2026-09-30"})
        _, b = await call(c, "get_price", {"commodity": "锂", "date": "2026-09-30"})
    assert not err
    assert a["source"] == "mock" and a["data_source"] == "mock"
    assert a["price"] == b["price"] > 0
    assert a["currency"] == "USD" and a["unit"] == "t"
    assert a["source_url"] and a["retrieved_at"]


async def test_get_price_weekend_returns_previous_trading_day():
    async with mcp_session(mcp) as c:
        _, r = await call(c, "get_price", {"commodity": "copper", "date": "2026-10-04"})  # 周日
    as_of = date.fromisoformat(r["as_of"])
    assert as_of.weekday() < 5 and as_of <= date(2026, 10, 4)


@pytest.mark.parametrize(
    "args",
    [
        {"commodity": "unobtainium", "date": "today"},
        {"commodity": "lithium", "date": "2026/10/01"},
        {"commodity": "lithium", "date": (date.today() + timedelta(days=3)).isoformat()},
    ],
)
async def test_get_price_errors_are_structured(args):
    async with mcp_session(mcp) as c:
        err, msg = await call(c, "get_price", args)
        # server 不会崩：报错后同一会话还能继续调用
        ok_err, _ = await call(c, "list_commodities")
    assert err and msg
    assert not ok_err


async def test_get_trend_stats_consistent():
    async with mcp_session(mcp) as c:
        err, t = await call(c, "get_trend", {"commodity": "lithium", "days": 30})
    assert not err
    s, series = t["stats"], t["series"]
    dates = [p["date"] for p in series]
    assert dates == sorted(dates) and len(series) >= 15
    assert s["first"] == series[0]["close"] and s["last"] == series[-1]["close"]
    assert s["change_pct"] == pytest.approx((s["last"] - s["first"]) / s["first"] * 100, abs=0.01)
    assert s["low"] <= s["ma7"] <= s["high"] and s["ma30"] is not None
    assert s["volatility_annualized_pct"] > 0
    assert t["source"] == "mock"


async def test_get_trend_7_days_and_bad_days():
    async with mcp_session(mcp) as c:
        err, t = await call(c, "get_trend", {"commodity": "copper", "days": 7})
        bad, _ = await call(c, "get_trend", {"commodity": "copper", "days": 1})
    assert not err and 3 <= t["stats"]["points"] <= 6
    assert bad


def _yahoo_payload():
    today = date.today()
    days = [today - timedelta(days=i) for i in range(20, -1, -1)]
    import calendar

    ts = [calendar.timegm(d.timetuple()) + 3600 * 20 for d in days]
    closes = [2500 + i for i in range(len(days))]
    closes[3] = None  # Yahoo 偶尔会有空值
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": closes}]}}]}}


async def test_live_yahoo_provider_parsed(monkeypatch):
    monkeypatch.setenv("MINING_OFFLINE", "0")
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        return httpx.Response(200, json=_yahoo_payload())

    monkeypatch.setattr(providers, "http_client", lambda **kw: httpx.Client(transport=httpx.MockTransport(handler)))
    async with mcp_session(mcp) as c:
        _, r = await call(c, "get_trend", {"commodity": "aluminium", "days": 14})
    assert "ALI=F" in seen["url"]
    assert r["source"] == "yahoo" and r["data_source"] == "live"
    assert r["stats"]["last"] == 2520
    assert all(p["close"] is not None for p in r["series"])


def _yahoo_sparse(points: list[tuple[int, float]]):
    """points: [(距今天数, 收盘价)]，模拟成交稀疏的合约。"""
    import calendar

    today = date.today()
    ts = [calendar.timegm((today - timedelta(days=d)).timetuple()) + 3600 * 20 for d, _ in points]
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": [v for _, v in points]}]}}]}}


async def test_lithium_yahoo_usd_per_kg_converted_and_single_request(monkeypatch):
    """LTH=F 报价是 USD/kg；且 get_price + 两次 get_trend 只应请求 Yahoo 一次（缓存），保证同一来源。"""
    monkeypatch.setenv("MINING_OFFLINE", "0")
    hits = []

    def handler(req: httpx.Request):
        hits.append(str(req.url))
        # 稀疏：最近 7 天内只有 1 个点，基准点在 9 天前
        return httpx.Response(200, json=_yahoo_sparse([(40, 13.0), (30, 14.0), (20, 14.5), (9, 14.8), (1, 15.07)]))

    monkeypatch.setattr(providers, "http_client", lambda **kw: httpx.Client(transport=httpx.MockTransport(handler)))
    async with mcp_session(mcp) as c:
        _, p = await call(c, "get_price", {"commodity": "lithium", "date": "today"})
        _, t7 = await call(c, "get_trend", {"commodity": "lithium", "days": 7})
        _, t30 = await call(c, "get_trend", {"commodity": "lithium", "days": 30})
    assert p["price"] == 15070 and p["unit"] == "t" and p["source"] == "yahoo"
    assert t7["source"] == t30["source"] == "yahoo"  # 没有因为数据稀疏降级到 mock
    assert t7["stats"]["first"] == 14800 and t7["stats"]["last"] == 15070  # 基准 = 7 天前最近的收盘价
    assert t7["start_date"] <= t7["window_start"]
    assert t30["stats"]["first"] == 14000
    assert len(hits) == 1 and "LTH=F" in hits[0] and "range=3mo" in hits[0]


async def test_concurrent_calls_single_flight_and_consistent_fallback(monkeypatch):
    """agent 会并发调 get_price + get_trend(7) + get_trend(30)：Yahoo 超时时只应请求一次，三者一致降级到 mock。"""
    import asyncio
    import time

    monkeypatch.setenv("MINING_OFFLINE", "0")
    monkeypatch.delenv("METALS_API_KEY", raising=False)
    hits = []

    def handler(req):
        hits.append(1)
        time.sleep(0.3)
        raise httpx.ConnectTimeout("handshake timed out")

    monkeypatch.setattr(providers, "http_client", lambda **kw: httpx.Client(transport=httpx.MockTransport(handler)))
    async with mcp_session(mcp) as c:
        rs = await asyncio.gather(
            call(c, "get_price", {"commodity": "lithium", "date": "today"}),
            call(c, "get_trend", {"commodity": "lithium", "days": 7}),
            call(c, "get_trend", {"commodity": "lithium", "days": 30}),
        )
    assert len(hits) == 1
    assert [r["source"] for _, r in rs] == ["mock", "mock", "mock"]
    assert all("ConnectTimeout" in r["note"] for _, r in rs)


async def test_live_failure_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("MINING_OFFLINE", "0")
    monkeypatch.delenv("METALS_API_KEY", raising=False)
    handler = lambda req: httpx.Response(429, text="Too Many Requests")  # noqa: E731
    monkeypatch.setattr(providers, "http_client", lambda **kw: httpx.Client(transport=httpx.MockTransport(handler)))
    async with mcp_session(mcp) as c:
        err, r = await call(c, "get_price", {"commodity": "aluminium", "date": "today"})
    assert not err
    assert r["source"] == "mock" and "已降级" in r["note"] and "429" in r["note"]

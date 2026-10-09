"""价格 provider 抽象层。

LME 没有免费 API，这里做成可插拔：
  - YahooProvider     Yahoo Finance 期货代码（免 key，非官方接口，可能被限流）
  - MetalsApiProvider metals-api.com（需 METALS_API_KEY）
  - MockProvider      确定性模拟序列，结果里明确标注 source=mock

换成付费 LME 数据源时只需新增一个 Provider，工具接口不变。
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from servers.common import http_client, is_offline

log = logging.getLogger("lme-price")


@dataclass(frozen=True)
class Commodity:
    key: str
    name: str
    unit: str
    currency: str
    yahoo: str | None  # Yahoo 期货代码
    metals_api: str | None  # metals-api 符号
    mock_base: float  # 模拟序列的长期均值
    mock_vol: float  # 模拟序列的日波动率
    yahoo_scale: float = 1.0  # Yahoo 报价换算到 unit 的倍数


COMMODITIES: dict[str, Commodity] = {
    c.key: c
    for c in [
        # CME 氢氧化锂期货 LTH 报价单位是 USD/kg，统一换算成 USD/t
        Commodity("lithium", "Lithium Hydroxide CIF CJK (CME)", "t", "USD", "LTH=F", "LITH", 15000, 0.022, yahoo_scale=1000),
        Commodity("spodumene", "Spodumene SC6 FOB Australia", "t", "USD", None, None, 850, 0.025),
        Commodity("aluminium", "Aluminium (COMEX)", "t", "USD", "ALI=F", "ALU", 2500, 0.012),
        Commodity("copper", "Copper (COMEX)", "lb", "USD", "HG=F", "XCU", 4.3, 0.013),
        Commodity("nickel", "Nickel (LME)", "t", "USD", None, "NI", 16500, 0.016),
        Commodity("iron_ore", "Iron Ore 62% Fe CFR China", "t", "USD", None, "IRON", 105, 0.015),
        Commodity("gold", "Gold (COMEX)", "oz", "USD", "GC=F", "XAU", 2400, 0.009),
    ]
}

ALIASES = {
    "li": "lithium", "lithium": "lithium", "锂": "lithium", "碳酸锂": "lithium", "氢氧化锂": "lithium",
    "lithium hydroxide": "lithium", "lithium carbonate": "lithium",
    "spodumene": "spodumene", "锂辉石": "spodumene", "sc6": "spodumene",
    "al": "aluminium", "aluminium": "aluminium", "aluminum": "aluminium", "铝": "aluminium",
    "cu": "copper", "copper": "copper", "铜": "copper",
    "ni": "nickel", "nickel": "nickel", "镍": "nickel",
    "iron ore": "iron_ore", "iron_ore": "iron_ore", "铁矿石": "iron_ore",
    "au": "gold", "gold": "gold", "黄金": "gold",
}


def resolve_commodity(name: str) -> Commodity:
    key = ALIASES.get(name.strip().lower().replace("-", " "))
    if key is None:
        raise KeyError(f"不支持的品种 '{name}'，可选：{', '.join(sorted(COMMODITIES))}")
    return COMMODITIES[key]


@dataclass
class Series:
    points: list[tuple[date, float]]  # 按日期升序
    source: str  # yahoo / metals-api / mock
    source_url: str
    data_source: str  # live / mock
    note: str = ""


class ProviderError(Exception):
    pass


class YahooProvider:
    name = "yahoo"
    BASE = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

    CACHE_TTL = 600  # 秒：同一 ticker 的 get_price / get_trend(7) / get_trend(30) 共用一次请求，避免限流和混源
    _cache: dict[tuple[str, str], tuple[float, list[tuple[date, float]]]] = {}
    _lock = threading.Lock()

    def supports(self, c: Commodity) -> bool:
        return c.yahoo is not None

    FAIL_TTL = 60  # 秒：失败也短暂缓存，让并发的 3 个调用一致地降级，而不是各自再超时一遍
    _key_locks: dict[tuple[str, str], threading.Lock] = {}

    def _fetch(self, c: Commodity, rng: str) -> list[tuple[date, float]]:
        key = (c.yahoo, rng)
        with self._lock:
            klock = self._key_locks.setdefault(key, threading.Lock())
        with klock:  # single-flight：同一 ticker 同时只发一个请求，其余等结果
            with self._lock:
                hit = self._cache.get(key)
            if hit:
                age = time.monotonic() - hit[0]
                if isinstance(hit[1], ProviderError):
                    if age < self.FAIL_TTL:
                        raise hit[1]
                elif age < self.CACHE_TTL:
                    return hit[1]
            try:
                pts = self._download(c, rng)
            except ProviderError as e:
                err = e
            except Exception as e:  # noqa: BLE001 —— 网络异常统一转成 ProviderError
                err = ProviderError(f"{type(e).__name__}: {e}")
            else:
                with self._lock:
                    self._cache[key] = (time.monotonic(), pts)
                return pts
            with self._lock:
                self._cache[key] = (time.monotonic(), err)
            raise err

    def _download(self, c: Commodity, rng: str) -> list[tuple[date, float]]:
        url = self.BASE.format(ticker=c.yahoo)
        with http_client(headers={"User-Agent": "Mozilla/5.0"}) as cli:
            r = cli.get(url, params={"range": rng, "interval": "1d"})
        if r.status_code != 200:
            raise ProviderError(f"yahoo HTTP {r.status_code}")
        try:
            res = r.json()["chart"]["result"][0]
            ts = res["timestamp"]
            closes = res["indicators"]["quote"][0]["close"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"yahoo 返回格式异常: {e}") from e
        pts = [
            (datetime.fromtimestamp(t, tz=timezone.utc).date(), round(float(v) * c.yahoo_scale, 4))
            for t, v in zip(ts, closes)
            if v is not None
        ]
        return pts

    def history(self, c: Commodity, start: date, end: date) -> Series:
        span = (date.today() - start).days  # Yahoo 的 range 是相对今天往前算的
        rng = "3mo" if span <= 88 else "1y" if span <= 360 else "5y"
        pts = [p for p in self._fetch(c, rng) if start <= p[0] <= end]
        if not pts:
            raise ProviderError("yahoo 无数据")
        return Series(pts, "yahoo", f"https://finance.yahoo.com/quote/{c.yahoo}", "live")


class MetalsApiProvider:
    name = "metals-api"
    BASE = "https://metals-api.com/api/timeseries"

    def __init__(self) -> None:
        self.key = os.environ.get("METALS_API_KEY", "")

    def supports(self, c: Commodity) -> bool:
        return bool(self.key) and c.metals_api is not None

    def history(self, c: Commodity, start: date, end: date) -> Series:
        with http_client() as cli:
            r = cli.get(
                self.BASE,
                params={
                    "access_key": self.key,
                    "base": "USD",
                    "symbols": c.metals_api,
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                },
            )
        data = r.json() if r.status_code == 200 else {}
        if not data.get("success"):
            raise ProviderError(f"metals-api 失败: HTTP {r.status_code} {str(data)[:120]}")
        pts = []
        for d, rates in sorted(data.get("rates", {}).items()):
            v = rates.get(c.metals_api)
            day = date.fromisoformat(d)
            if v and start <= day <= end:  # metals-api 返回 1 USD 可买多少单位，取倒数
                pts.append((day, round(1.0 / float(v), 4)))
        if not pts:
            raise ProviderError("metals-api 无数据")
        return Series(pts, "metals-api", "https://metals-api.com/", "live")


class MockProvider:
    """确定性模拟：同一品种、同一日期永远得到同一个价格（便于测试和复现）。

    对数均值回归随机游走，从 ANCHOR 起逐个交易日生成，种子 = sha256(品种)。
    """

    name = "mock"
    ANCHOR = date(2024, 1, 1)

    def supports(self, c: Commodity) -> bool:
        return True

    @staticmethod
    def _trading_days(start: date, end: date):
        d = start
        while d <= end:
            if d.weekday() < 5:
                yield d
            d += timedelta(days=1)

    def history(self, c: Commodity, start: date, end: date) -> Series:
        seed = int(hashlib.sha256(c.key.encode()).hexdigest()[:12], 16)
        rnd = random.Random(seed)
        mu = math.log(c.mock_base)
        x = mu
        pts = []
        for d in self._trading_days(self.ANCHOR, end):
            x += 0.03 * (mu - x) + c.mock_vol * rnd.gauss(0, 1)
            if d >= start:
                pts.append((d, round(math.exp(x), 4 if c.mock_base < 100 else 2)))
        if not pts:
            raise ProviderError("mock 区间内无交易日（起始日期早于 2024-01-01 或区间过短）")
        return Series(pts, "mock", "mock://lme-price/" + c.key, "mock", note="模拟数据，仅供演示，不代表真实行情")


def provider_chain() -> list:
    if is_offline():
        return [MockProvider()]
    return [YahooProvider(), MetalsApiProvider(), MockProvider()]


def fetch_history(c: Commodity, start: date, end: date, min_points: int = 1) -> Series:
    errors = []
    for p in provider_chain():
        if not p.supports(c):
            continue
        try:
            s = p.history(c, start, end)
            if len(s.points) < min_points:
                errors.append(f"{p.name}: 区间内数据点不足（{len(s.points)} < {min_points}）")
                continue
            if errors:
                s.note = "; ".join(filter(None, [s.note, "已降级：" + " | ".join(errors)]))
            return s
        except Exception as e:  # noqa: BLE001 —— 任何 provider 失败都降级到下一个
            log.warning("provider %s 失败: %s", p.name, e)
            errors.append(f"{p.name}: {e}")
    raise ProviderError("所有 provider 均失败: " + " | ".join(errors))

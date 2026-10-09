"""Agent 离线端到端：不联网、不配 LLM，也必须生成结构完整、引用有效的简报。"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agent import analysis
from agent.graph import run_briefing
from agent.mcp_clients import connect
from agent.report import CITE_RE, check_markdown

ROOT = Path(__file__).resolve().parent.parent
QUERY = "给我生成一份关于 Pilbara 锂矿的今日简报"
SECTIONS = ["## 要点速览", "## 新闻摘要", "## 储量数据", "## 价格走势", "## 风险提示", "## 引用源"]
EMPTY_STATE = {"- 暂无", "- 过去 7 天未检索到相关新闻", "- 未识别到显著风险信号"}  # 空状态提示不是结论，无需引用


def assert_valid_report(md: str, citations: list[dict]):
    for s in SECTIONS:
        assert s in md, s
    assert check_markdown(md, citations) == []
    body, _, refs = md.partition("## 引用源")
    used = sorted({int(x) for x in CITE_RE.findall(body)})
    assert used == list(range(1, len(citations) + 1))  # 连续编号、无多余条目
    assert len(re.findall(r"^\d+\. ", refs, re.M)) == len(citations)
    # 每条叙述性结论都带引用
    for sec in ("## 要点速览", "## 新闻摘要", "## 风险提示"):
        block = md.split(sec, 1)[1].split("\n## ", 1)[0]
        for line in [ln for ln in block.splitlines() if ln.startswith("- ") and ln not in EMPTY_STATE]:
            assert CITE_RE.search(line), f"{sec} 缺引用：{line}"


async def test_offline_briefing_end_to_end():
    async with connect("memory") as hub:
        st = await run_briefing(QUERY, hub)
    p = st["plan"]
    assert (p["region"], p["commodity"]) == ("Pilbara", "lithium")
    assert set(p["projects"]) == {"Pilgangoora", "Wodgina"}
    assert st["attempt"] == 0 and st["missing"] == []
    md = st["report_md"]
    assert md.startswith(f"# Pilbara 锂矿日报 ({p['date']})")
    assert_valid_report(md, st["citations"])
    assert len(st["news"]) >= 3 and len(st["reserves"]) == 2 and set(st["prices"]) == {"lithium", "spodumene"}
    assert "| Pilgangoora | JORC | Indicated | 251.6 |" in md
    assert "mock" in md and "离线快照" in md  # 数据来源被诚实标注
    # 新闻优先选 Pilbara / 项目相关的
    assert any(w in st["news"][0]["title"] for w in ("Pilbara", "Pilgangoora", "Wodgina", "PLS"))
    # 3 个 gather 并行：同一轮各只调用一次 list/extract
    tools = [c["tool"] for c in hub.calls]
    assert tools.count("list_reports") == 1 and tools.count("extract_resources") == 2


class FakeLLM:
    enabled = True
    model = "fake-llm"

    async def plan(self, query, known, fallback):
        return {**fallback, "planner": "llm"}

    async def qualitative_risks(self, state, valid_ids):
        if not state["news"]:
            return []
        return [{"level": "中", "text": "LLM 补充风险", "cites": [state["news"][0]["cite"]]}]

    async def write_narrative(self, state, fallback):
        return {
            section: [{"text": "LLM：" + it["text"], "cites": it["cites"]} for it in items]
            for section, items in fallback.items()
        }


async def test_llm_mode_runs_through_graph():
    async with connect("memory") as hub:
        st = await run_briefing(QUERY, hub, FakeLLM())
    assert st["plan"]["planner"] == "llm"
    assert "LLM（fake-llm）" in st["report_md"]
    assert "LLM：" in st["report_md"]
    assert any(r.get("rule") == "llm" for r in st["risks"])
    assert_valid_report(st["report_md"], st["citations"])


class LiveSpotOnlyHub:
    """模拟真实情况：锂现价来自 Yahoo（live），但历史不足导致 get_trend 降级为 mock。"""

    def __init__(self, hub):
        self.hub = hub

    async def call(self, server, tool, args=None):
        r = await self.hub.call(server, tool, args)
        if server == "price" and args.get("commodity") == "lithium" and tool == "get_price":
            r = {**r, "source": "yahoo", "data_source": "live", "price": 15070.0, "source_url": "https://finance.yahoo.com/quote/LTH=F"}
        return r


async def test_live_spot_with_mock_trend_is_not_mixed():
    async with connect("memory") as hub:
        st = await run_briefing(QUERY, LiveSpotOnlyHub(hub))
    li = st["prices"]["lithium"]
    assert li["trend7"] is None and li["trend30"] is None
    row = next(ln for ln in st["report_md"].splitlines() if ln.startswith("| 锂（"))
    assert "15,070.00 USD/t" in row and "yahoo（实时）" in row and "混合" not in row
    assert row.count("| — |") >= 2
    assert any("涨跌幅留空" in n for n in st["notes"])
    assert not any(r["rule"].startswith("price_drop") and r["cites"] == [li["cite"]] for r in st["risks"])
    assert_valid_report(st["report_md"], st["citations"])


class FlakyNewsHub:
    """包装真实 hub：7 天窗口的新闻检索永远为空，用来触发补查。"""

    def __init__(self, hub, always_empty=False):
        self.hub, self.always_empty, self.calls = hub, always_empty, []

    async def call(self, server, tool, args=None):
        self.calls.append((server, tool, dict(args or {})))
        r = await self.hub.call(server, tool, args)
        if server == "news" and tool == "search" and (self.always_empty or args.get("days") == 7):
            return {**r, "results": [], "count": 0}
        return r


async def test_missing_news_triggers_regather_with_wider_window():
    async with connect("memory") as hub:
        flaky = FlakyNewsHub(hub)
        st = await run_briefing(QUERY, flaky)
    assert st["attempt"] == 1 and st["news"]
    days = {a["days"] for s, t, a in flaky.calls if t == "search"}
    assert days == {7, 30}
    # 补查只重跑缺失的板块
    assert sum(1 for s, t, a in flaky.calls if t == "extract_resources") == 2
    assert_valid_report(st["report_md"], st["citations"])


async def test_regather_is_bounded_and_report_still_rendered():
    async with connect("memory") as hub:
        flaky = FlakyNewsHub(hub, always_empty=True)
        st = await run_briefing(QUERY, flaky)
    assert st["attempt"] == 2 and st["missing"] == ["news"]
    assert "过去 7 天未检索到相关新闻" in st["report_md"]
    assert sum(1 for s, t, a in flaky.calls if t == "search" and a["days"] == 30) > 0
    assert_valid_report(st["report_md"], st["citations"])


def test_rule_plan_variants():
    known = [
        {"project": "Pilgangoora", "region": "Pilbara", "commodity": "lithium"},
        {"project": "Wodgina", "region": "Pilbara", "commodity": "lithium"},
    ]
    p = analysis.rule_plan("Wodgina 2026-09-30 的锂矿情况", known)
    assert p["date"] == "2026-09-30" and "Wodgina" in p["projects"] and p["region"] == "Pilbara"
    p2 = analysis.rule_plan("pilbara copper briefing", known)
    assert p2["commodity"] == "copper" and p2["projects"] == [] and p2["price_commodities"] == ["copper"]


def test_rule_risks_thresholds():
    st = {
        "prices": {"lithium": {"cite": 1, "spot": {"data_source": "live"}, "trend30": {"stats": {"change_pct": -12.0, "volatility_annualized_pct": 70}}, "trend7": {"stats": {"change_pct": -6.0}}}},
        "reserves": [{"project": "X", "cite": 2, "confidence": 0.5, "page": 3, "categories": [{"category": "Inferred", "tonnage_mt": 40}, {"category": "Total", "tonnage_mt": 100}]}],
        "news": [{"title": "Miner to curtail output and cut 200 jobs", "text": "", "cite": 3}, {"title": "WA lifts lithium royalty", "text": "", "cite": 4}],
    }
    rules = {r["rule"] for r in analysis.rule_risks(st)}
    assert {"price_drop_30d", "price_drop_7d", "volatility", "inferred_share", "parse_confidence", "news:减产/维护停机", "news:裁员", "news:特许权费/政策"} <= rules
    assert analysis.rule_risks(st)[0]["level"] == "高"


def test_cli_offline_stdio(tmp_path):
    """真实子进程 + stdio 传输，跟评审方跑的命令一致。"""
    out = tmp_path / "b.md"
    r = subprocess.run(
        [sys.executable, "-m", "agent.cli", "--offline", "--out", str(out), QUERY],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120, env={**__import__("os").environ, "MCP_TRANSPORT": "stdio"},
    )
    assert r.returncode == 0, r.stderr
    md = out.read_text(encoding="utf-8")
    assert md.strip() == r.stdout.strip()
    for s in SECTIONS:
        assert s in md

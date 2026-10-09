"""LangGraph 状态图：plan → (gather_news ∥ gather_reserves ∥ gather_prices) → analyze → write → validate_citations。

validate_citations 发现某个板块数据缺失时，最多回到对应 gather 节点补查 2 轮（第二轮会放宽检索条件）。
"""
from __future__ import annotations

import asyncio
import logging
import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent import analysis
from agent.mcp_clients import MCPHub, ToolCallError
from agent.report import DS_LABEL, CitationRegistry, finalize, price_data_sources, price_sources

log = logging.getLogger("agent.graph")
MAX_RETRY = 2
GATHER_NODES = {"news": "gather_news", "reserves": "gather_reserves", "prices": "gather_prices"}


class BriefingState(TypedDict, total=False):
    query: str
    plan: dict
    news: list[dict]
    reserves: list[dict]
    prices: dict[str, dict]
    citations: list[dict]
    risks: list[dict]
    narrative: dict
    notes: list[str]
    meta: dict
    report_md: str
    attempt: int
    missing: list[str]
    validation: list[str]
    errors: Annotated[list[str], operator.add]


def _ds_summary(values: list[str]) -> str:
    vals = sorted(set(v for v in values if v))
    return "/".join(DS_LABEL.get(v, v) for v in vals) if vals else "—"


def build_graph(hub: MCPHub, llm=None):
    """llm 为 None 或 llm.enabled=False 时全程走规则 + 离线模板。"""
    use_llm = bool(llm and getattr(llm, "enabled", False))

    async def safe(server: str, tool: str, args: dict, errors: list[str]):
        try:
            return await hub.call(server, tool, args)
        except ToolCallError as e:
            errors.append(str(e)[:200])
            return None

    # ------------------------------------------------------------ plan
    async def plan(state: BriefingState) -> dict:
        errors: list[str] = []
        known = (await safe("pdf", "list_reports", {}, errors) or {}).get("projects", [])
        p = analysis.rule_plan(state["query"], known)
        if use_llm:
            try:
                p = await llm.plan(state["query"], known, p)
            except Exception as e:  # noqa: BLE001
                errors.append(f"LLM plan 失败，使用规则结果：{e}"[:200])
        log.info("plan: %s", {k: p[k] for k in ("region", "commodity", "projects", "date")})
        return {"plan": p, "attempt": 0, "errors": errors}

    # ------------------------------------------------------------ gather
    async def gather_news(state: BriefingState) -> dict:
        p, attempt, errors = state["plan"], state.get("attempt", 0), []
        days = 7 if attempt == 0 else 30  # 补查时放宽时间窗
        queries = p["news_queries"] if attempt == 0 else p["news_queries"] + [p["commodity"]]
        results = await asyncio.gather(*(safe("news", "search", {"query": q, "days": days, "limit": 8}, errors) for q in queries))
        flat = [it for r in results if r for it in r["results"]]
        picked = analysis.pick_news(flat, p, top_n=6)
        arts = await asyncio.gather(*(safe("news", "fetch_article", {"url": it["url"], "max_chars": 4000}, errors) for it in picked))
        news = []
        for it, a in zip(picked, arts):
            a = a or {}
            news.append(
                {
                    **it,
                    "text": a.get("text") or it.get("snippet", ""),
                    "text_source": a.get("text_source", "snippet"),
                    "resolved_url": a.get("resolved_url", it["url"]),
                    "data_source": a.get("data_source", it.get("data_source")),
                }
            )
        return {"news": news, "errors": errors}

    async def gather_reserves(state: BriefingState) -> dict:
        p, errors = state["plan"], []

        async def one(name: str):
            meta = p.get("project_meta", {}).get(name, {})
            urls = [u for u in (meta.get("pdf_url"), meta.get("fixture_url") or f"fixture://{name.lower()}") if u]
            for u in urls:
                r = await safe("pdf", "extract_resources", {"pdf_url": u}, errors)
                if r:
                    return {**r, "project": r.get("project") or name}
            return None

        got = await asyncio.gather(*(one(n) for n in p["projects"]))
        return {"reserves": [r for r in got if r], "errors": errors}

    async def gather_prices(state: BriefingState) -> dict:
        p, errors = state["plan"], []

        async def one(c: str):
            spot, t7, t30 = await asyncio.gather(
                safe("price", "get_price", {"commodity": c, "date": p["date"]}, errors),
                safe("price", "get_trend", {"commodity": c, "days": 7}, errors),
                safe("price", "get_trend", {"commodity": c, "days": 30}, errors),
            )
            entry = {"spot": spot, "trend7": t7, "trend30": t30}
            # 现价是实时的、但实时源没有足够历史（如 LTH=F 只有最新结算价）而趋势降级成了 mock：
            # 不拿模拟走势冒充真实走势，宁可留空
            if spot and spot.get("data_source") == "live":
                dropped = [k for k in ("trend7", "trend30") if (entry[k] or {}).get("data_source") == "mock"]
                for k in dropped:
                    entry[k] = None
                if dropped:
                    entry["note"] = f"{spot['name']}：实时源（{spot['source']}）仅提供最新价、无足够历史数据，涨跌幅留空（不使用模拟数据代替）。"
            return c, entry

        got = await asyncio.gather(*(one(c) for c in p["price_commodities"]))
        return {"prices": {c: v for c, v in got if v["spot"]}, "errors": errors}

    # ------------------------------------------------------------ analyze
    async def analyze(state: BriefingState) -> dict:
        reg = CitationRegistry()
        news = []
        for n in state.get("news", []):
            cid = reg.add(title=n["title"], url=n["url"], source=n.get("source", ""), published_at=n.get("published_at"), retrieved_at=n.get("retrieved_at"), data_source=n.get("data_source") or "live", kind="news")
            news.append({**n, "cite": cid})
        reserves = []
        for r in state.get("reserves", []):
            title = f"{r['project']} Mineral Resource Statement（{r.get('reporting_code') or '未识别规范'}，第 {r.get('page')} 页）"
            cid = reg.add(title=title, url=r["source_url"], source="技术报告 PDF", retrieved_at=r.get("retrieved_at"), data_source=r.get("data_source", "live"), kind="report")
            reserves.append({**r, "cite": cid})
        prices = {}
        for k, p in state.get("prices", {}).items():
            sp = p["spot"]
            cid = reg.add(title=f"{sp['name']} 价格（{sp['source']}）", url=sp["source_url"], source=sp["source"], retrieved_at=sp.get("retrieved_at"), data_source=sp.get("data_source", "live"), kind="price")
            prices[k] = {**p, "cite": cid}

        partial = {**state, "news": news, "reserves": reserves, "prices": prices}
        risks = analysis.rule_risks(partial)
        errors: list[str] = []
        if use_llm:
            try:
                extra = await llm.qualitative_risks({**partial, "risks": risks}, reg.ids())
                risks += [{**r, "rule": "llm"} for r in extra]
            except Exception as e:  # noqa: BLE001
                errors.append(f"LLM 定性风险失败：{e}"[:200])

        notes = []
        if any(n.get("data_source") == "fixture" for n in news):
            notes.append("新闻来自离线快照（真实 Google News RSS，抓取时间见引用源），非当日实时检索。")
        if any(r.get("data_source") == "fixture" for r in reserves):
            notes.append("储量数据来自仓库内样例 PDF（示意数值，非官方披露）；在 fixtures/reports/index.json 填入真实报告链接即可解析真实数据。")
        if any("mock" in price_data_sources(p) for p in prices.values()):
            notes.append("价格为确定性模拟序列（source=mock）：LME 无免费 API，接入付费数据源后接口不变。基于模拟价格的风险信号仅用于演示规则。")
        notes += [p["note"] for p in prices.values() if p.get("note")]
        mixed = [k for k, p in prices.items() if len(price_sources(p)) > 1]
        if mixed:
            notes.append(f"{'、'.join(mixed)} 的现价与涨跌幅来自不同数据源（部分实时请求失败后降级），表中已标注“混合”，请谨慎比较。")
        notes.append("Pilbara 锂项目多为澳洲上市公司，按 JORC 规范披露（非加拿大 NI 43-101），两者 Measured / Indicated / Inferred 分类基本一致。")
        meta = {
            "news_ds": _ds_summary([n.get("data_source") for n in news]),
            "reserves_ds": _ds_summary([r.get("data_source") for r in reserves]),
            "prices_ds": _ds_summary([d for p in prices.values() for d in price_data_sources(p)]),
            "mode": f"LLM（{llm.model}）" if use_llm else "离线模板（未配置 LLM）",
        }
        return {"news": news, "reserves": reserves, "prices": prices, "citations": reg.to_list(), "risks": risks, "notes": notes, "meta": meta, "errors": errors}

    # ------------------------------------------------------------ write
    async def write(state: BriefingState) -> dict:
        narrative = analysis.offline_narrative(state)
        if use_llm:
            try:
                narrative = await llm.write_narrative(state, narrative)
                fb = getattr(llm, "last_fallback_sections", [])
                if fb:
                    return {"narrative": narrative, "errors": [f"LLM 输出的 {fb} 板块无有效引用，已用离线模板替代"]}
            except Exception as e:  # noqa: BLE001
                meta = {**state.get("meta", {}), "mode": f"离线模板（LLM {llm.model} 调用失败，已回退）"}
                return {"narrative": narrative, "meta": meta, "errors": [f"LLM 写作失败，使用离线模板：{e}"[:200]]}
        return {"narrative": narrative}

    # ------------------------------------------------------------ validate_citations
    async def validate_citations(state: BriefingState) -> dict:
        md, patch, issues = finalize(state)
        p = state["plan"]
        fetchable = {"news": True, "reserves": bool(p.get("projects")), "prices": bool(p.get("price_commodities"))}
        missing = [k for k in ("news", "reserves", "prices") if not state.get(k) and fetchable[k]]
        attempt = state.get("attempt", 0)
        if missing and attempt < MAX_RETRY:
            log.info("数据缺失 %s，第 %d 轮补查", missing, attempt + 1)
            return {"missing": missing, "attempt": attempt + 1, "validation": issues}
        return {**patch, "report_md": md, "missing": [] if not missing else missing, "validation": issues, "attempt": attempt}

    def route(state: BriefingState):
        if state.get("report_md"):
            return END
        return [GATHER_NODES[m] for m in state["missing"]]

    g = StateGraph(BriefingState)
    for name, fn in [("plan", plan), ("gather_news", gather_news), ("gather_reserves", gather_reserves), ("gather_prices", gather_prices), ("analyze", analyze), ("write", write), ("validate_citations", validate_citations)]:
        g.add_node(name, fn)
    g.add_edge(START, "plan")
    for n in GATHER_NODES.values():
        g.add_edge("plan", n)
        g.add_edge(n, "analyze")  # 分开的边：补查时只跑一个 gather 也能触发 analyze
    g.add_edge("analyze", "write")
    g.add_edge("write", "validate_citations")
    g.add_conditional_edges("validate_citations", route, [*GATHER_NODES.values(), END])
    return g.compile()


async def run_briefing(query: str, hub: MCPHub, llm=None) -> dict[str, Any]:
    graph = build_graph(hub, llm)
    return await graph.ainvoke({"query": query, "errors": []}, {"recursion_limit": 40})

"""规则部分：计划抽取、新闻排序、风险信号、离线叙述生成。全部确定性，可单测。"""
from __future__ import annotations

import re
from datetime import date

from agent.report import COMMODITY_ZH, DS_LABEL, fmt_num, fmt_pct, price_data_sources, short_date

REGIONS = {
    "pilbara": "Pilbara", "皮尔巴拉": "Pilbara", "皮尔布拉": "Pilbara",
    "goldfields": "Goldfields", "james bay": "James Bay", "atacama": "Atacama", "阿塔卡马": "Atacama",
}
COMMODITY_WORDS = [
    (r"锂|lithium|spodumene|锂辉石", "lithium"),
    (r"铜|copper", "copper"),
    (r"镍|nickel", "nickel"),
    (r"铁矿|iron ore", "iron_ore"),
    (r"黄金|金矿|gold", "gold"),
]
PRICE_SET = {"lithium": ["lithium", "spodumene"]}

# 风险关键词：(正则, 标签, 等级)
RISK_KEYWORDS = [
    (r"\b(suspend\w*|halt\w*)\b|停产|暂停", "停产/暂停", "高"),
    (r"\bcurtail\w*|care and maintenance|\bproduction cut\w*|\bcut\w* (output|production)|减产", "减产/维护停机", "高"),
    (r"\blay ?offs?\b|\bjob cuts?\b|\bcut\w*\s+(?:[\d,]+\s+)?(?:jobs|roles|staff|workers)\b|\bredundanc\w*|\bretrench\w*|裁员", "裁员", "中"),
    (r"\broyalt\w*|特许权费|权利金", "特许权费/政策", "中"),
    (r"\bstrike\b|\bindustrial action\b|罢工", "劳资纠纷", "中"),
    (r"\bimpairment\b|\bwrite-?downs?\b|减值", "资产减值", "中"),
    (r"\bdowngrade\w*|\bestimate cut\b|\beps\b.*\bcut\b|下调", "盈利预期下调", "低"),
    (r"\bsues?\b|\blawsuit\b|\blitigation\b|诉讼", "法律纠纷", "低"),
]

PRICE_DROP_HIGH, PRICE_DROP_MED, VOL_HIGH, INFERRED_HIGH = -10.0, -5.0, 60.0, 0.30


# ---------------------------------------------------------------- plan（规则版）
def rule_plan(query: str, known_projects: list[dict], today: date | None = None) -> dict:
    q = query.lower()
    region = next((v for k, v in REGIONS.items() if k in q), None)
    commodity = next((c for rx, c in COMMODITY_WORDS if re.search(rx, q)), "lithium")
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", query)
    d = m.group(1) if m else (today or date.today()).isoformat()
    projects = []
    for p in known_projects:
        named = p["project"].lower() in q
        if named or ((region is None or p.get("region") == region) and p.get("commodity") == commodity):
            projects.append(p)
    region = region or (projects[0].get("region") if projects else "全球")
    return {
        "region": region,
        "commodity": commodity,
        "projects": [p["project"] for p in projects],
        "project_meta": {p["project"]: p for p in projects},
        "date": d,
        "news_queries": [f"{region} {commodity}"] + [p["project"] for p in projects][:3],
        "price_commodities": PRICE_SET.get(commodity, [commodity]),
        "planner": "rules",
    }


# ---------------------------------------------------------------- 新闻
def key_terms(plan: dict) -> dict[str, int]:
    """地区 / 项目 / 运营方命中权重 2，品种词权重 1 —— 优先选真正和 Pilbara 项目相关的新闻。"""
    terms: dict[str, int] = {plan["commodity"].lower(): 1}
    if plan["commodity"] == "lithium":
        terms["spodumene"] = 1
    if plan.get("region"):
        terms[plan["region"].lower()] = 2
    for name in plan["projects"]:
        terms[name.lower()] = 2
        meta = plan.get("project_meta", {}).get(name, {})
        for alias in meta.get("aliases") or []:
            if len(alias) >= 3:
                terms[alias.lower()] = 2
    return terms


def news_score(item: dict, terms: dict[str, int]) -> int:
    hay = (item.get("title", "") + " " + item.get("snippet", "")).lower()
    return sum(w for t, w in terms.items() if re.search(rf"\b{re.escape(t)}\b", hay))


def pick_news(results: list[dict], plan: dict, top_n: int = 6) -> list[dict]:
    terms = key_terms(plan)
    seen, items = set(), []
    for it in results:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        s = news_score(it, terms)
        if s > 0:
            items.append({**it, "score": s})
    items.sort(key=lambda x: (x["score"], x.get("published_at") or ""), reverse=True)
    return items[:top_n]


# ---------------------------------------------------------------- 风险规则
def rule_risks(state: dict) -> list[dict]:
    risks: list[dict] = []
    for key, p in state.get("prices", {}).items():
        zh = COMMODITY_ZH.get(key, key)
        mock = "（注意：含模拟数据，仅演示规则）" if "mock" in price_data_sources(p) else ""
        t30, t7 = p.get("trend30"), p.get("trend7")
        if t30 and t30["stats"]["change_pct"] is not None:
            ch = t30["stats"]["change_pct"]
            if ch <= PRICE_DROP_HIGH:
                risks.append({"level": "高", "rule": "price_drop_30d", "text": f"{zh}价格 30 天累计下跌 {ch:.1f}%，超过 {abs(PRICE_DROP_HIGH):.0f}% 阈值，矿山现金流承压{mock}", "cites": [p["cite"]]})
            elif ch <= PRICE_DROP_MED:
                risks.append({"level": "中", "rule": "price_drop_30d", "text": f"{zh}价格 30 天下跌 {ch:.1f}%{mock}", "cites": [p["cite"]]})
        if t7 and t7["stats"]["change_pct"] is not None and t7["stats"]["change_pct"] <= PRICE_DROP_MED:
            risks.append({"level": "中", "rule": "price_drop_7d", "text": f"{zh}价格 7 天下跌 {t7['stats']['change_pct']:.1f}%，短期走弱{mock}", "cites": [p["cite"]]})
        vol = (t30 or {}).get("stats", {}).get("volatility_annualized_pct")
        if vol and vol >= VOL_HIGH:
            risks.append({"level": "中", "rule": "volatility", "text": f"{zh}价格 30 天年化波动率 {vol:.0f}%，波动剧烈{mock}", "cites": [p["cite"]]})

    for r in state.get("reserves", []):
        cats = {c["category"]: c for c in r["categories"]}
        total = cats.get("Total")
        inf = cats.get("Inferred")
        if total and inf and total["tonnage_mt"]:
            share = inf["tonnage_mt"] / total["tonnage_mt"]
            if share > INFERRED_HIGH:
                risks.append({"level": "中", "rule": "inferred_share", "text": f"{r['project']} 资源量中 Inferred（推断）占比 {share:.0%}，高于 {INFERRED_HIGH:.0%}，地质置信度偏低，转化为储量存在不确定性", "cites": [r["cite"]]})
        if r.get("confidence", 1) < 0.7:
            risks.append({"level": "低", "rule": "parse_confidence", "text": f"{r['project']} 储量表自动解析置信度仅 {r['confidence']:.2f}，建议人工复核原文第 {r.get('page')} 页", "cites": [r["cite"]]})

    by_label: dict[str, dict] = {}
    for n in state.get("news", []):
        hay = f"{n.get('title', '')} {n.get('text', '')}"
        for rx, label, level in RISK_KEYWORDS:
            if re.search(rx, hay, re.I):
                b = by_label.setdefault(label, {"titles": [], "cites": [], "level": level})
                b["titles"].append(n["title"])
                b["cites"].append(n["cite"])
    for label, b in by_label.items():
        ex = "；".join(f"“{t[:80]}{'…' if len(t) > 80 else ''}”" for t in b["titles"][:2])
        risks.append({"level": b["level"], "rule": f"news:{label}", "text": f"新闻出现「{label}」信号：{ex}", "cites": b["cites"][:3]})
    order = {"高": 0, "中": 1, "低": 2}
    return sorted(risks, key=lambda x: order.get(x["level"], 3))


# ---------------------------------------------------------------- 离线叙述
def first_sentence(text: str, limit: int = 140) -> str:
    s = re.split(r"(?<=[.!?。！？])\s", (text or "").strip(), maxsplit=1)[0]
    return s[:limit] + ("…" if len(s) > limit else "")


def offline_narrative(state: dict) -> dict:
    news, reserves, prices, risks = state.get("news", []), state.get("reserves", []), state.get("prices", {}), state.get("risks", [])
    highlights = []
    for key, p in prices.items():
        sp, t30 = p.get("spot"), p.get("trend30")
        if sp:
            ch = fmt_pct(t30["stats"]["change_pct"]) if t30 else "—"
            highlights.append({"text": f"{COMMODITY_ZH.get(key, key)}最新 {fmt_num(sp['price'], 2)} {sp['currency']}/{sp['unit']}，30 天 {ch}（{DS_LABEL.get(sp['data_source'])}数据）", "cites": [p["cite"]]})
            break
    if reserves:
        tot = [next((c for c in r["categories"] if c["category"] == "Total"), None) for r in reserves]
        mt = sum(t["tonnage_mt"] for t in tot if t)
        kt = sum(t["contained"] or 0 for t in tot if t)
        names = "、".join(r["project"] for r in reserves)
        highlights.append({"text": f"已解析 {len(reserves)} 个项目（{names}）资源量，合计 {fmt_num(mt)} Mt、金属量约 {fmt_num(kt, 0)} kt {reserves[0].get('commodity') or ''}".strip(), "cites": [r["cite"] for r in reserves]})
    if news:
        top = news[0]
        hl = f"最受关注：{top['title']}（{top.get('source', '')}）"
        if risks:
            hl += f"；共识别 {len(risks)} 条风险信号"
        highlights.append({"text": hl, "cites": [top["cite"]]})

    news_items = []
    for n in news:
        extra = first_sentence(n.get("text", "")) if n.get("text_source") == "full" else ""
        line = f"**{n['title']}**（{n.get('source', '')}，{short_date(n.get('published_at'))}）"
        if extra and extra.lower() not in n["title"].lower():
            line += f"：{extra}"
        news_items.append({"text": line, "cites": [n["cite"]]})

    risk_items = [{"text": f"【{r['level']}】{r['text']}", "cites": r["cites"]} for r in risks]
    return {"highlights": highlights[:3], "news": news_items, "risks": risk_items}

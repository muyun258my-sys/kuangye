"""引用登记表 + Markdown 渲染 + 引用校验。

原则：
  - 数字（储量表、价格表）一律由代码从工具结果渲染，不交给 LLM，避免数字幻觉
  - 叙述性内容（要点 / 新闻摘要 / 风险）每条必须带 [n]，n 必须在登记表里
  - validate_citations 剔除无效编号、丢弃没有任何有效引用的结论，再按首次出现顺序重新编号
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

CITE_RE = re.compile(r"\[(\d{1,3})\]")

COMMODITY_ZH = {"lithium": "锂", "spodumene": "锂辉石", "copper": "铜", "nickel": "镍", "aluminium": "铝", "iron_ore": "铁矿石", "gold": "黄金"}
DS_LABEL = {"live": "实时", "fixture": "离线快照", "mock": "模拟"}


# ---------------------------------------------------------------- 引用登记表
@dataclass
class Citation:
    id: int
    title: str
    url: str
    source: str = ""
    published_at: str | None = None
    retrieved_at: str | None = None
    data_source: str = "live"
    kind: str = "news"  # news / report / price


class CitationRegistry:
    def __init__(self, items: list[dict] | None = None):
        self._by_url: dict[str, Citation] = {}
        for it in items or []:
            c = Citation(**it)
            self._by_url[c.url] = c

    def add(self, *, title: str, url: str, **kw) -> int:
        if url in self._by_url:
            return self._by_url[url].id
        c = Citation(id=len(self._by_url) + 1, title=title or url, url=url, **kw)
        self._by_url[url] = c
        return c.id

    def ids(self) -> set[int]:
        return {c.id for c in self._by_url.values()}

    def get(self, i: int) -> Citation | None:
        return next((c for c in self._by_url.values() if c.id == i), None)

    def to_list(self) -> list[dict]:
        return [asdict(c) for c in sorted(self._by_url.values(), key=lambda c: c.id)]


def cite(ids) -> str:
    return "".join(f"[{i}]" for i in sorted(set(ids)) if i)


# ---------------------------------------------------------------- 格式化
def fmt_num(v, nd=1) -> str:
    if v is None:
        return "—"
    return f"{v:,.{nd}f}"


def fmt_pct(v) -> str:
    if v is None:
        return "—"
    arrow = "▲" if v > 0 else "▼" if v < 0 else "■"
    return f"{arrow} {v:+.2f}%"


def short_date(s: str | None) -> str:
    return (s or "")[:10]


def md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def _link(c: dict) -> str:
    title = md_escape(c["title"]).replace("[", "(").replace("]", ")")
    if c["url"].startswith(("http://", "https://")):
        return f"[{title}]({c['url']})"
    return f"{title} (`{c['url']}`)"


# ---------------------------------------------------------------- 渲染
def reserves_table(reserves: list[dict]) -> list[str]:
    lines = ["| 项目 | 规范 | 类别 | 矿石量 (Mt) | 品位 | 金属量 | 来源 |", "|---|---|---|---|---|---|---|"]
    for r in reserves:
        for c in r["categories"]:
            cat = f"**{c['category']}**" if c["category"] == "Total" else c["category"]
            derived = "*" if c.get("derived") or c.get("contained_derived") else ""
            lines.append(
                f"| {md_escape(r['project'])} | {r.get('reporting_code') or '—'} | {cat} | {fmt_num(c['tonnage_mt'])} | "
                f"{c['grade']:.2f}{c.get('unit') or ''} | {fmt_num(c['contained'], 0)} {c.get('contained_unit', '')}{derived} | {cite([r['cite']])} |"
            )
    return lines


def price_sources(p: dict) -> set[str]:
    """一个品种的现价 / 7 天 / 30 天分别来自哪些 provider。"""
    return {x.get("source") for x in (p.get("spot"), p.get("trend7"), p.get("trend30")) if x and x.get("source")}


def price_data_sources(p: dict) -> set[str]:
    return {x.get("data_source") for x in (p.get("spot"), p.get("trend7"), p.get("trend30")) if x and x.get("data_source")}


def prices_table(prices: dict) -> list[str]:
    lines = ["| 品种 | 最新价 | 7 天变化 | 30 天变化 | 30 天区间 | 数据来源 |", "|---|---|---|---|---|---|"]
    for key, p in prices.items():
        spot, t7, t30 = p.get("spot"), p.get("trend7"), p.get("trend30")
        if not spot:
            continue
        unit = f"{spot['currency']}/{spot['unit']}"
        rng = f"{fmt_num(t30['stats']['low'], 2)}–{fmt_num(t30['stats']['high'], 2)}" if t30 else "—"
        srcs = price_sources(p)
        if len(srcs) == 1:
            src = f"{spot['source']}（{DS_LABEL.get(spot['data_source'], spot['data_source'])}）"
        else:  # 同一行的数字来自不同 provider：必须明确告诉读者
            src = "⚠ 混合：现价 {} / 7天 {} / 30天 {}".format(*[(x or {}).get("source", "—") for x in (spot, t7, t30)])
        lines.append(
            f"| {COMMODITY_ZH.get(key, key)}（{md_escape(spot['name'])}） | {fmt_num(spot['price'], 2)} {unit}（{spot['as_of']}） | "
            f"{fmt_pct(t7['stats']['change_pct']) if t7 else '—'} | {fmt_pct(t30['stats']['change_pct']) if t30 else '—'} | {rng} | "
            f"{src}{cite([p['cite']])} |"
        )
    return lines


def render(state: dict) -> str:
    plan = state["plan"]
    nar = state["narrative"]  # {highlights, news, risks}: [{text, cites}]
    reserves, prices = state.get("reserves", []), state.get("prices", {})
    region, commodity = plan["region"], COMMODITY_ZH.get(plan["commodity"], plan["commodity"])
    out = [f"# {region} {commodity}矿日报 ({plan['date']})", ""]

    meta = state.get("meta", {})
    out.append(
        f"> 生成方式：{meta.get('mode', '离线模板')}　|　数据来源：新闻 {meta.get('news_ds', '—')} · 储量 {meta.get('reserves_ds', '—')} · 价格 {meta.get('prices_ds', '—')}"
    )
    out.append("")

    def bullets(items, empty):
        if not items:
            return [f"- {empty}"]
        return [f"- {it['text']} {cite(it['cites'])}".rstrip() for it in items]

    out += ["## 要点速览", *bullets(nar.get("highlights", [])[:3], "暂无"), ""]
    out += ["## 新闻摘要", *bullets(nar.get("news", []), "过去 7 天未检索到相关新闻"), ""]
    out += ["## 储量数据"]
    if reserves:
        out += reserves_table(reserves)
        out += ["", "<sub>储量口径为 Mineral Resource（含 Ore Reserve）。带 * 的金属量 / 合计由矿石量 × 品位推算。</sub>"]
    else:
        out += ["- 未获取到技术报告资源量数据"]
    out += [""]
    out += ["## 价格走势"]
    out += prices_table(prices) if prices else ["- 未获取到价格数据"]
    out += [""]
    out += ["## 风险提示", *bullets(nar.get("risks", []), "未识别到显著风险信号"), ""]

    notes = state.get("notes", [])
    if notes:
        out += ["## 数据说明", *[f"- {n}" for n in notes], ""]

    out += ["## 引用源"]
    for c in state["citations"]:
        when = short_date(c.get("published_at")) or f"获取于 {short_date(c.get('retrieved_at'))}"
        tag = "" if c["data_source"] == "live" else f"（{DS_LABEL.get(c['data_source'], c['data_source'])}）"
        out.append(f"{c['id']}. {_link(c)} — {md_escape(c.get('source') or '')}, {when}{tag}")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- 引用校验
def validate_narrative(narrative: dict, valid_ids: set[int]) -> tuple[dict, list[str]]:
    """剔除不存在的引用编号；没有任何有效引用的结论直接丢弃（防幻觉）。"""
    issues, out = [], {}
    for section, items in narrative.items():
        kept = []
        for it in items or []:
            text = CITE_RE.sub("", it.get("text", "")).strip()  # 正文里内嵌的 [n] 统一移到 cites
            inline = [int(x) for x in CITE_RE.findall(it.get("text", ""))]
            cites = [int(x) for x in list(it.get("cites") or []) + inline if str(x).isdigit()]
            bad = sorted({c for c in cites if c not in valid_ids})
            good = sorted({c for c in cites if c in valid_ids})
            if bad:
                issues.append(f"{section}: 剔除无效引用 {bad}")
            if not good:
                issues.append(f"{section}: 丢弃无有效引用的结论：{text[:40]}")
                continue
            kept.append({**it, "text": text, "cites": good})
        out[section] = kept
    return out, issues


def renumber(markdown: str, citations: list[dict]) -> tuple[dict[int, int], list[dict]]:
    """按正文首次出现顺序把引用重新编号为 1..k，并只保留被引用过的条目。"""
    body, _, _ = markdown.partition("\n## 引用源")
    order: list[int] = []
    for m in CITE_RE.finditer(body):
        n = int(m.group(1))
        if n not in order:
            order.append(n)
    by_id = {c["id"]: c for c in citations}
    order = [o for o in order if o in by_id]
    mapping = {old: new for new, old in enumerate(order, 1)}
    return mapping, [{**by_id[o], "id": mapping[o]} for o in order]


def finalize(state: dict) -> tuple[str, dict, list[str]]:
    """validate_citations 节点的核心：校验 → 渲染 → 重新编号 → 再渲染 → 最终检查。

    返回 (markdown, 更新后的 state 片段, issues)。
    """
    valid = {c["id"] for c in state["citations"]}
    narrative, issues = validate_narrative(state["narrative"], valid)
    draft = render({**state, "narrative": narrative})
    mapping, new_cits = renumber(draft, state["citations"])

    def remap(ids):
        return [mapping[i] for i in ids if i in mapping]

    narrative = {k: [{**it, "cites": remap(it["cites"])} for it in v] for k, v in narrative.items()}
    reserves = [{**r, "cite": mapping.get(r["cite"])} for r in state.get("reserves", [])]
    prices = {k: {**p, "cite": mapping.get(p["cite"])} for k, p in state.get("prices", {}).items()}
    patch = {"narrative": narrative, "reserves": reserves, "prices": prices, "citations": new_cits}
    md = render({**state, **patch})
    issues += check_markdown(md, new_cits)
    return md, patch, issues


def check_markdown(markdown: str, citations: list[dict]) -> list[str]:
    """最终兜底检查：正文中每个 [n] 都必须出现在引用源列表里。"""
    body, _, _ = markdown.partition("\n## 引用源")
    ids = {c["id"] for c in citations}
    return [f"正文引用 [{n}] 不在引用源中" for n in sorted({int(x) for x in CITE_RE.findall(body)}) if n not in ids]

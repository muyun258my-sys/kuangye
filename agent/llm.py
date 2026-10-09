"""OpenAI 兼容 LLM 客户端。

读取 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，可接 OpenAI、DeepSeek、Qwen、vLLM、Ollama 等任何
OpenAI Chat Completions 兼容接口。未配置 LLM_API_KEY 时 enabled=False，agent 自动走离线规则 + 模板。

分工（防幻觉）：
  - plan               LLM 只能从已登记项目里挑，日期 / 品种做白名单校验
  - qualitative_risks  LLM 只补充定性风险，规则风险由代码算，LLM 输出的引用编号逐条校验
  - write_narrative    LLM 只写「要点速览」「新闻摘要」；储量表、价格表、风险列表由代码渲染
所有输出都先用 JSON mode 请求；服务端不支持 response_format 时自动退回普通模式。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from typing import Any

import openai
from openai import AsyncOpenAI

from agent.analysis import PRICE_SET

log = logging.getLogger("agent.llm")

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
ALLOWED_COMMODITIES = {"lithium", "spodumene", "aluminium", "copper", "nickel", "iron_ore", "gold"}

PLAN_SYSTEM = """你是矿权日报的任务规划器。根据用户需求，从 known_projects 里挑选相关项目。
只输出一个 JSON 对象，格式：
{"region": "地区英文名，如 Pilbara", "commodity": "lithium|spodumene|copper|nickel|aluminium|iron_ore|gold 之一", "projects": ["只能取 known_projects 里的 project 值"], "date": "YYYY-MM-DD"}
规则：不得新造项目；用户没指定日期就用 today；拿不准的字段沿用 rule_plan 的值。"""

RISK_SYSTEM = """你是矿业风险分析师。facts 里每条数据都带 cite 编号，rule_risks 是规则已识别的风险（不要重复）。
请补充 0-3 条定性风险（供需格局、政策与特许权费、项目执行、融资与市场情绪等），只输出 JSON：
{"risks": [{"level": "高|中|低", "text": "不超过 60 字的中文", "cites": [编号]}]}
约束：每条必须有 facts 依据，cites 只能取 valid_cites 中的编号；data_source 为 mock 的价格是模拟数据，不能据此下结论；没有可靠依据就返回 {"risks": []}。"""

WRITE_SYSTEM = """你是矿权日报主笔，用中文写两个板块，只输出 JSON：
{"highlights": [{"text": "...", "cites": [编号]}],   // 正好 3 条，最重要的结论
 "news": [{"text": "...", "cites": [编号]}]}         // facts.news 每条新闻 1 条，概括要点，不超过 80 字
约束：
1. 只使用 facts 中的信息，数字必须与 facts 完全一致，不得编造；
2. cites 只能取 valid_cites 里的编号，text 里不要写 [n]；
3. 储量表、价格表、风险列表由程序生成，不要输出；
4. data_source 为 mock / fixture 的数据要在句中注明“模拟”或“离线快照”；
5. draft 是规则生成的初稿，可以在其基础上改写润色。"""


def _parse_json(text: str | None) -> Any:
    """从模型输出里抠出 JSON；模型常在前后加说明文字或 ```json 围栏。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"[\[{].*[\]}]", s, re.S)
        if m:
            return json.loads(m.group(0))
        raise ValueError("模型输出不是有效 JSON") from None


def _clean_items(items: Any, valid_ids: set[int]) -> list[dict]:
    """只保留 text 非空且至少有一个有效引用的条目；正文里内嵌的 [n] 去掉（编号统一放 cites）。"""
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        text = re.sub(r"\s*\[\d{1,3}\]", "", str(it.get("text", ""))).strip()
        raw = it.get("cites", [])
        raw = raw if isinstance(raw, list) else [raw]
        cites = [int(c) for c in raw if isinstance(c, int) or (isinstance(c, str) and c.strip().isdigit())]
        good = sorted({c for c in cites if c in valid_ids})
        if text and good:
            out.append({"text": text, "cites": good})
    return out


def _digest(state: dict) -> dict:
    """喂给 LLM 的事实摘要：只放带 cite 编号的字段，控制 token。"""
    news = [
        {
            "cite": n["cite"],
            "title": n["title"],
            "source": n.get("source"),
            "published_at": (n.get("published_at") or "")[:10],
            "text": (n.get("text") or "")[:600],
            "data_source": n.get("data_source"),
        }
        for n in state.get("news", [])
    ]
    reserves = [
        {
            "cite": r["cite"],
            "project": r["project"],
            "reporting_code": r.get("reporting_code"),
            "cutoff": r.get("cutoff"),
            "data_source": r.get("data_source"),
            "categories": [
                {k: c.get(k) for k in ("category", "tonnage_mt", "grade", "unit", "contained", "contained_unit")}
                for c in r.get("categories", [])
            ],
        }
        for r in state.get("reserves", [])
    ]
    prices = []
    for key, p in state.get("prices", {}).items():
        spot, t7, t30 = p.get("spot") or {}, p.get("trend7") or {}, p.get("trend30") or {}
        prices.append(
            {
                "cite": p["cite"],
                "commodity": key,
                "price": spot.get("price"),
                "unit": f"{spot.get('currency')}/{spot.get('unit')}",
                "as_of": spot.get("as_of"),
                "change_pct_7d": t7.get("stats", {}).get("change_pct"),
                "change_pct_30d": t30.get("stats", {}).get("change_pct"),
                "data_source": sorted({x.get("data_source") for x in (spot, t7, t30) if x}),
            }
        )
    return {"news": news, "reserves": reserves, "prices": prices}


class LLM:
    def __init__(
        self,
        enabled: bool = False,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = "",
        temperature: float = 0.2,
        http_client=None,
    ):
        self.enabled = bool(enabled and api_key)
        self.model = model or DEFAULT_MODEL
        self.temperature = temperature
        self.json_mode = True  # 服务端不支持 response_format 时自动关闭
        self.last_fallback_sections: list[str] = []
        self.client = (
            AsyncOpenAI(base_url=base_url or DEFAULT_BASE_URL, api_key=api_key, timeout=60, max_retries=2, http_client=http_client)
            if self.enabled
            else None
        )

    @classmethod
    def from_env(cls, disabled: bool = False) -> "LLM":
        api_key = os.environ.get("LLM_API_KEY", "").strip()
        return cls(
            enabled=not disabled and bool(api_key),
            model=os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL,
            base_url=os.environ.get("LLM_BASE_URL", "").strip() or DEFAULT_BASE_URL,
            api_key=api_key,
        )

    async def _complete(self, system: str, user: str) -> str:
        if self.client is None:
            raise RuntimeError("LLM 未启用")
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.temperature,
        }
        if self.json_mode:
            try:
                resp = await self.client.chat.completions.create(**kwargs, response_format={"type": "json_object"})
            except openai.BadRequestError as e:  # 部分本地模型 / 老版本服务不认 response_format
                log.info("服务端不支持 JSON mode，改用普通模式：%s", e)
                self.json_mode = False
                resp = await self.client.chat.completions.create(**kwargs)
        else:
            resp = await self.client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        if not content:
            raise RuntimeError("模型返回空内容")
        return content

    async def _json(self, system: str, payload: dict) -> dict:
        data = _parse_json(await self._complete(system, json.dumps(payload, ensure_ascii=False, default=str)))
        if not isinstance(data, dict):
            raise ValueError("模型输出必须是 JSON 对象")
        return data

    # ------------------------------------------------------------ plan
    async def plan(self, query: str, known: list[dict], fallback: dict) -> dict:
        projects = [{k: p.get(k) for k in ("project", "aliases", "region", "commodity")} for p in known]
        rule = {k: fallback[k] for k in ("region", "commodity", "projects", "date")}
        data = await self._json(
            PLAN_SYSTEM, {"query": query, "known_projects": projects, "today": date.today().isoformat(), "rule_plan": rule}
        )
        allowed = {p["project"] for p in known}
        chosen = [p for p in (data.get("projects") or []) if p in allowed] or list(fallback["projects"])
        commodity = re.sub(r"[-\s]+", "_", str(data.get("commodity", "")).strip().lower())
        if commodity not in ALLOWED_COMMODITIES:
            commodity = fallback["commodity"]
        region = str(data.get("region") or "").strip() or fallback["region"]
        d = str(data.get("date", "")).strip()
        try:
            d = date.fromisoformat(d).isoformat()
        except ValueError:
            d = fallback["date"]
        queries = [f"{region} {commodity}"] + chosen[:3]
        return {
            "region": region,
            "commodity": commodity,
            "projects": chosen,
            "project_meta": {p["project"]: p for p in known if p["project"] in chosen},
            "date": d,
            "news_queries": queries,
            "price_commodities": PRICE_SET.get(commodity, [commodity]),
            "planner": "llm",
        }

    # ------------------------------------------------------------ analyze
    async def qualitative_risks(self, state: dict, valid_ids: set[int]) -> list[dict]:
        rule_risks = [{"level": r["level"], "text": r["text"]} for r in state.get("risks", [])]
        data = await self._json(
            RISK_SYSTEM, {"facts": _digest(state), "rule_risks": rule_risks, "valid_cites": sorted(valid_ids)}
        )
        out = []
        for it in (data.get("risks") or [])[:3]:
            if not isinstance(it, dict):
                continue
            level = str(it.get("level", "中")).strip()
            cleaned = _clean_items([it], valid_ids)
            if cleaned:
                out.append({"level": level if level in {"高", "中", "低"} else "中", **cleaned[0]})
        return out

    # ------------------------------------------------------------ write
    async def write_narrative(self, state: dict, fallback: dict) -> dict:
        """LLM 写要点和新闻；风险列表沿用规则 + 定性风险（由代码渲染，防止 LLM 漏掉规则风险）。"""
        valid_ids = {c["id"] for c in state.get("citations", [])}
        data = await self._json(
            WRITE_SYSTEM,
            {
                "plan": {k: state["plan"].get(k) for k in ("region", "commodity", "projects", "date")},
                "facts": _digest(state),
                "valid_cites": sorted(valid_ids),
                "draft": {k: fallback.get(k, []) for k in ("highlights", "news")},
            },
        )
        self.last_fallback_sections = []
        out: dict[str, list[dict]] = {}
        for section in ("highlights", "news"):
            cleaned = _clean_items(data.get(section), valid_ids)
            if section == "highlights":
                cleaned = cleaned[:3]
            if not cleaned:
                self.last_fallback_sections.append(section)
            out[section] = cleaned or fallback.get(section, [])
        out["risks"] = fallback.get("risks", [])
        return out

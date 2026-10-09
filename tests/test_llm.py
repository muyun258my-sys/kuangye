"""LLM 客户端单元测试：只测本地 JSON 解析 / 引用清洗 / 环境开关，不联网。"""
from types import SimpleNamespace

from agent import analysis
from agent.llm import LLM, _clean_items, _parse_json

KNOWN = [
    {"project": "Pilgangoora", "aliases": ["pls"], "region": "Pilbara", "commodity": "lithium"},
    {"project": "Wodgina", "aliases": ["marbl"], "region": "Pilbara", "commodity": "lithium"},
]


def _client(content: str):
    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _llm(content: str) -> LLM:
    llm = LLM(enabled=True, api_key="test")
    llm.client = _client(content)
    return llm


def test_from_env_off_by_default(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert LLM.from_env().enabled is False


def test_from_env_enabled_with_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    llm = LLM.from_env()
    assert llm.enabled is True and llm.model == "deepseek-chat"


def test_parse_json_strips_fence_and_prose():
    assert _parse_json("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert _parse_json("好的，结果如下：{\"a\": 1}") == {"a": 1}


def test_clean_items_keeps_only_valid_cites():
    assert _clean_items([{"text": "x", "cites": [1, 2]}, {"text": "y", "cites": ["3", 99]}, {"text": "", "cites": [1]}], {1, 2, 3}) == [
        {"text": "x", "cites": [1, 2]},
        {"text": "y", "cites": [3]},
    ]


async def test_plan_selects_only_known_projects():
    fallback = analysis.rule_plan("Pilbara 锂矿", KNOWN)
    llm = _llm('{"region":"Pilbara","commodity":"lithium","projects":["Pilgangoora"],"date":"2026-10-09"}')
    p = await llm.plan("Pilbara 锂矿", KNOWN, fallback)
    assert p["projects"] == ["Pilgangoora"]
    assert set(p["project_meta"]) == {"Pilgangoora"}
    assert p["planner"] == "llm" and p["price_commodities"] == ["lithium", "spodumene"]


async def test_qualitative_risks_filters_bad_cites_and_levels():
    state = {"news": [{"cite": 1, "title": "a", "text": ""}], "reserves": [], "prices": {}}
    llm = _llm('{"risks":[{"level":"高","text":"风险 A","cites":[1,99]},{"level":"超高","text":"风险 B","cites":[2]}]}')
    assert await llm.qualitative_risks(state, {1}) == [{"level": "高", "text": "风险 A", "cites": [1]}]


async def test_real_openai_client_json_mode_fallback_and_full_graph():
    """用真实 openai SDK + 假 HTTP 服务端跑完整个图：验证请求格式、JSON mode 降级、引用清洗。"""
    import json as _json

    import httpx

    from agent.graph import run_briefing
    from agent.mcp_clients import connect
    from agent.report import CITE_RE, check_markdown

    seen = {"requests": 0, "rejected_json_mode": 0, "systems": []}

    def handler(req: httpx.Request):
        body = _json.loads(req.content)
        assert req.url.path.endswith("/chat/completions") and body["model"] == "test-model"
        assert req.headers["authorization"] == "Bearer sk-test"
        seen["requests"] += 1
        if "response_format" in body:  # 模拟不支持 JSON mode 的服务端
            seen["rejected_json_mode"] += 1
            return httpx.Response(400, json={"error": {"message": "response_format is not supported", "type": "invalid_request_error"}})
        system, user = body["messages"][0]["content"], _json.loads(body["messages"][1]["content"])
        seen["systems"].append(system[:10])
        assert "JSON" in system
        if "规划器" in system:
            out = {"region": "Pilbara", "commodity": "lithium", "projects": ["Pilgangoora", "Atlantis"], "date": "2026-10-09"}
        elif "风险分析师" in system:
            n0 = user["facts"]["news"][0]["cite"]
            out = {"risks": [{"level": "中", "text": "锂辉石供应合同增多，议价能力分化", "cites": [n0]}, {"level": "高", "text": "编造的风险", "cites": [999]}]}
        else:
            ids = user["valid_cites"]
            out = {
                "highlights": [{"text": f"LLM 要点 {i} [77]", "cites": [ids[0], 77]} for i in range(5)],
                "news": [{"text": "LLM 新闻：" + n["title"][:30], "cites": [n["cite"]]} for n in user["facts"]["news"]],
            }
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "test-model", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "```json\n" + _json.dumps(out, ensure_ascii=False) + "\n```"}}]})

    llm = LLM(enabled=True, model="test-model", base_url="http://llm.test/v1", api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async with connect("memory") as hub:
        st = await run_briefing("给我生成一份关于 Pilbara 锂矿的今日简报", hub, llm)

    md = st["report_md"]
    assert seen["rejected_json_mode"] == 1 and llm.json_mode is False  # 只试一次 JSON mode，之后不再带
    assert seen["requests"] == 4  # plan + risks + write，外加一次被拒的 JSON mode
    assert st["plan"]["projects"] == ["Pilgangoora"]  # 编造的 Atlantis 被过滤
    assert "LLM（test-model）" in md and "LLM 新闻：" in md
    assert md.count("LLM 要点") == 3  # 要点截断到 3 条
    assert "编造的风险" not in md and "议价能力分化" in md
    assert "[77]" not in md and "[999]" not in md
    assert check_markdown(md, st["citations"]) == []
    body = md.split("## 引用源")[0]
    assert sorted({int(x) for x in CITE_RE.findall(body)}) == list(range(1, len(st["citations"]) + 1))
    # 规则风险没有被 LLM 覆盖掉
    assert "Inferred（推断）占比" in md


async def test_write_narrative_falls_back_for_empty_sections():
    state = {
        "plan": {"region": "Pilbara", "commodity": "lithium", "date": "2026-10-09"},
        "citations": [{"id": 1, "url": "https://x"}],
        "news": [{"cite": 1, "title": "a", "text": ""}],
        "reserves": [],
        "prices": {},
        "risks": [],
    }
    fallback = {
        "highlights": [{"text": "离线要点", "cites": [1]}],
        "news": [{"text": "离线新闻", "cites": [1]}],
        "risks": [{"text": "离线风险", "cites": [1]}],
    }
    llm = _llm('{"highlights":[],"news":[{"text":"LLM 新闻","cites":[1]}],"risks":[]}')
    out = await llm.write_narrative(state, fallback)
    assert out["highlights"] == fallback["highlights"]
    assert out["news"] == [{"text": "LLM 新闻", "cites": [1]}]
    assert out["risks"] == fallback["risks"]

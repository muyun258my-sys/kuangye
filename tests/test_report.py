from agent.report import CITE_RE, CitationRegistry, check_markdown, finalize, validate_narrative


def test_registry_dedupes_by_url():
    reg = CitationRegistry()
    a = reg.add(title="A", url="https://a")
    b = reg.add(title="B", url="https://b")
    assert (a, b, reg.add(title="A again", url="https://a")) == (1, 2, 1)
    assert CitationRegistry(reg.to_list()).ids() == {1, 2}


def test_validate_strips_hallucinated_cites_and_drops_uncited():
    nar = {
        "highlights": [{"text": "价格下跌 [2][99]", "cites": [1]}, {"text": "无出处的结论", "cites": [42]}],
        "risks": [{"text": "没有引用", "cites": []}],
    }
    out, issues = validate_narrative(nar, {1, 2})
    assert out["highlights"] == [{"text": "价格下跌", "cites": [1, 2]}]
    assert out["risks"] == []
    assert any("99" in i for i in issues) and any("无出处" in i for i in issues)


def _state():
    reg = CitationRegistry()
    n_unused = reg.add(title="Unused", url="https://unused")  # 1：没被引用，应被删掉
    n_news = reg.add(title="News", url="https://news", source="Reuters", published_at="2026-10-07T00:00:00+00:00")
    n_pdf = reg.add(title="Report", url="fixture://pilgangoora", data_source="fixture", kind="report")
    n_px = reg.add(title="Price", url="mock://lme-price/lithium", data_source="mock", kind="price")
    assert n_unused == 1
    return {
        "plan": {"region": "Pilbara", "commodity": "lithium", "date": "2026-10-08"},
        "citations": reg.to_list(),
        "narrative": {
            "highlights": [{"text": "要点", "cites": [n_px]}],
            "news": [{"text": "新闻", "cites": [n_news, 77]}],
            "risks": [{"text": "幻觉风险", "cites": [88]}],
        },
        "reserves": [
            {
                "project": "Pilgangoora",
                "reporting_code": "JORC",
                "cite": n_pdf,
                "categories": [{"category": "Indicated", "tonnage_mt": 251.6, "grade": 1.18, "unit": "% Li2O", "contained": 2969.0, "contained_unit": "kt Li2O"}],
            }
        ],
        "prices": {},
    }


def test_mixed_price_sources_are_flagged():
    from agent.report import prices_table

    spot = {"source": "yahoo", "data_source": "live", "name": "Li", "price": 15070, "currency": "USD", "unit": "t", "as_of": "2026-10-08"}
    trend = {"source": "mock", "data_source": "mock", "stats": {"change_pct": 1.0, "low": 1, "high": 2}}
    row = prices_table({"lithium": {"cite": 1, "spot": spot, "trend7": trend, "trend30": trend}})[-1]
    assert "⚠ 混合：现价 yahoo / 7天 mock / 30天 mock" in row
    same = prices_table({"lithium": {"cite": 1, "spot": spot, "trend7": {**trend, "source": "yahoo", "data_source": "live"}, "trend30": None}})[-1]
    assert "混合" not in same and "yahoo（实时）" in same


def test_finalize_renumbers_in_order_of_appearance():
    md, patch, issues = finalize(_state())
    body, _, refs = md.partition("## 引用源")
    nums = [int(x) for x in CITE_RE.findall(body)]
    assert sorted(set(nums)) == [1, 2, 3]
    assert [c["title"] for c in patch["citations"]] == ["Price", "News", "Report"]  # 要点→新闻→储量
    assert "Unused" not in refs and "幻觉风险" not in md
    assert "fixture://pilgangoora" in refs and "（离线快照）" in refs
    assert "| Pilgangoora | JORC | Indicated | 251.6 | 1.18% Li2O | 2,969 kt Li2O | [3] |" in md
    assert check_markdown(md, patch["citations"]) == []
    assert any("77" in i for i in issues)

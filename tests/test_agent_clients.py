import pytest

from agent.mcp_clients import ToolCallError, connect

EXPECTED = {
    "news": {"search", "fetch_article"},
    "pdf": {"extract_resources", "list_reports"},
    "price": {"get_price", "get_trend", "list_commodities"},
}


@pytest.mark.parametrize("transport", ["memory", "stdio"])
async def test_hub_connects_all_three_servers(transport):
    async with connect(transport) as hub:
        tools = await hub.list_tools()
        assert {k: set(v) for k, v in tools.items()} == EXPECTED
        r = await hub.call("price", "get_price", {"commodity": "lithium", "date": "today"})
        assert r["source"] == "mock"
        with pytest.raises(ToolCallError) as ei:
            await hub.call("pdf", "extract_resources", {"pdf_url": "fixture://nope"})
        assert ei.value.server == "pdf" and "未登记" in ei.value.message
        # 出错后会话仍可用
        r2 = await hub.call("news", "search", {"query": "Wodgina", "limit": 2})
        assert r2["count"] >= 1
    assert [c["isError"] for c in hub.calls] == [False, True, False]

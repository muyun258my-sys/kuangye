import base64
from datetime import datetime, timezone

import httpx
import pytest

from helpers import call, mcp_session
from servers.mining_news import server as news_server
from servers.mining_news import sources as src
from servers.mining_news.server import mcp

RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>
<item><title>Pilbara Minerals lifts Pilgangoora lithium output - Reuters</title>
<link>https://news.google.com/rss/articles/AAA?oc=5</link><pubDate>{d}</pubDate>
<description>&lt;a href="x"&gt;Pilbara Minerals lifts Pilgangoora lithium output&lt;/a&gt;&amp;nbsp;&lt;font&gt;Reuters&lt;/font&gt;</description>
<source url="https://www.reuters.com">Reuters</source></item>
<item><title>Unrelated football result - ESPN</title>
<link>https://news.google.com/rss/articles/BBB?oc=5</link><pubDate>{d}</pubDate>
<description>football</description><source url="https://espn.com">ESPN</source></item>
</channel></rss>"""

GDELT_JSON = {
    "articles": [
        {"url": "https://example.com/wodgina", "title": "Wodgina lithium mine curtails production", "seendate": "{s}", "domain": "example.com"},
        # 与 RSS 第一条标题相同 → 应被去重
        {"url": "https://example.com/dup", "title": "Pilbara Minerals lifts Pilgangoora lithium output", "seendate": "{s}", "domain": "example.com"},
    ]
}

ARTICLE_HTML = """<html><head><title>Wodgina lithium mine curtails production</title>
<meta property="og:site_name" content="Example News"></head><body><article>
<h1>Wodgina lithium mine curtails production</h1>
<p>The Wodgina lithium operation in Western Australia's Pilbara region will curtail spodumene production,
the joint venture partners said on Monday, citing weak lithium prices.</p>
<p>About 200 roles will be affected as one of three processing trains is placed on care and maintenance.
The partners said the decision would be reviewed quarterly as market conditions evolve.</p>
<p>Analysts expect the move to tighten spodumene supply over the next two quarters.</p>
</article></body></html>"""


def _now_strs():
    now = datetime.now(timezone.utc)
    return now.strftime("%a, %d %b %Y %H:%M:%S GMT"), now.strftime("%Y%m%dT%H%M%SZ")


def _patch_http(monkeypatch, handler):
    factory = lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)  # noqa: E731
    monkeypatch.setattr(src, "http_client", factory)
    monkeypatch.setattr(news_server, "http_client", factory)


async def test_list_tools():
    async with mcp_session(mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert {"search", "fetch_article"} <= set(tools)
    assert "query" in tools["search"].inputSchema["properties"]


async def test_offline_search_uses_real_snapshot():
    async with mcp_session(mcp) as c:
        err, r = await call(c, "search", {"query": "Pilbara lithium", "days": 7, "limit": 8})
    assert not err
    assert r["data_source"] == "fixture" and "离线快照" in r["note"]
    res = r["results"]
    assert 1 <= len(res) <= 8
    rels = [x["relevance"] for x in res]
    assert rels == sorted(rels, reverse=True) and rels[-1] > 0
    for x in res:
        assert x["url"].startswith("https://") and x["source_url"] == x["url"]
        assert x["retrieved_at"] and x["title"] and x["data_source"] == "fixture"


async def test_offline_fetch_article_falls_back_to_snapshot():
    async with mcp_session(mcp) as c:
        _, r = await call(c, "search", {"query": "Pilgangoora", "limit": 1})
        url = r["results"][0]["url"]
        err, a = await call(c, "fetch_article", {"url": url})
    assert not err
    assert a["url"] == url and a["title"] and a["text"]
    assert a["data_source"] == "fixture" and a["text_source"] == "snippet"


@pytest.mark.parametrize("url", ["ftp://x.com/a", "https://not-in-snapshot.example/xyz"])
async def test_fetch_article_errors_are_structured(url):
    async with mcp_session(mcp) as c:
        err, msg = await call(c, "fetch_article", {"url": url})
        ok_err, _ = await call(c, "search", {"query": "lithium"})
    assert err and msg and not ok_err


async def test_empty_query_is_error():
    async with mcp_session(mcp) as c:
        err, _ = await call(c, "search", {"query": "  "})
    assert err


async def test_live_search_merges_google_and_gdelt(monkeypatch):
    monkeypatch.setenv("MINING_OFFLINE", "0")
    d, s = _now_strs()
    gd = {"articles": [{**a, "seendate": s} for a in GDELT_JSON["articles"]]}

    def handler(req: httpx.Request):
        if req.url.host == "news.google.com":
            assert "when%3A7d" in str(req.url) or "when:7d" in str(req.url)
            return httpx.Response(200, text=RSS.format(d=d))
        if req.url.host == "api.gdeltproject.org":
            return httpx.Response(200, json=gd)
        return httpx.Response(404)

    _patch_http(monkeypatch, handler)
    async with mcp_session(mcp) as c:
        _, r = await call(c, "search", {"query": "Pilbara lithium Wodgina", "days": 7})
    titles = [x["title"] for x in r["results"]]
    assert r["data_source"] == "live"
    assert "Unrelated football result" not in titles  # 无关结果被过滤
    assert len([t for t in titles if "Pilgangoora" in t]) == 1  # 跨源去重
    first = next(x for x in r["results"] if "Pilgangoora" in x["title"])
    assert first["source"] == "Reuters" and first["snippet"] == ""
    assert any(x["provider"] == "gdelt" for x in r["results"])


async def test_tools_run_concurrently_and_schema_is_preserved(monkeypatch):
    """同步工具已放到线程里：4 个各耗时 0.6s 的检索并发执行，总耗时应远小于 2.4s。"""
    import asyncio
    import time

    monkeypatch.setenv("MINING_OFFLINE", "0")
    d, _ = _now_strs()

    def slow_google(query, days):
        time.sleep(0.6)
        return src.parse_google_rss(RSS.format(d=d))

    monkeypatch.setattr(src, "google_news", slow_google)
    monkeypatch.setattr(src, "gdelt", lambda q, days: [])
    async with mcp_session(mcp) as c:
        props = {t.name: t.inputSchema["properties"] for t in (await c.list_tools()).tools}
        t0 = time.perf_counter()
        res = await asyncio.gather(*(call(c, "search", {"query": f"Pilgangoora lithium {i}"}) for i in range(4)))
        elapsed = time.perf_counter() - t0
    assert set(props["search"]) == {"query", "days", "limit"} and set(props["fetch_article"]) == {"url", "max_chars"}
    assert all(not err for err, _ in res)
    assert elapsed < 1.6, f"工具被串行执行了：{elapsed:.2f}s"


async def test_live_search_all_fail_falls_back_to_fixture(monkeypatch):
    monkeypatch.setenv("MINING_OFFLINE", "0")
    _patch_http(monkeypatch, lambda req: httpx.Response(503))
    async with mcp_session(mcp) as c:
        err, r = await call(c, "search", {"query": "Pilbara lithium"})
    assert not err and r["data_source"] == "fixture"
    assert "联网失败" in r["note"] and r["count"] > 0


async def test_live_fetch_article_extracts_full_text(monkeypatch):
    monkeypatch.setenv("MINING_OFFLINE", "0")
    monkeypatch.setattr(src, "assert_public_http_url", lambda u: None)
    monkeypatch.setattr(news_server.src, "assert_public_http_url", lambda u: None)
    _patch_http(monkeypatch, lambda req: httpx.Response(200, html=ARTICLE_HTML))
    async with mcp_session(mcp) as c:
        err, a = await call(c, "fetch_article", {"url": "https://example.com/wodgina"})
    assert not err, a
    assert a["data_source"] == "live" and a["text_source"] == "full"
    assert "care and maintenance" in a["text"] and "Wodgina" in a["title"]


def test_resolve_old_style_google_news_url():
    raw = b"\x08\x13\x22\x20https://www.reuters.com/markets/lithium-1\xd2\x01\x00"
    art = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert src.resolve_google_news_url(f"https://news.google.com/rss/articles/{art}?oc=5") == "https://www.reuters.com/markets/lithium-1"
    assert src.resolve_google_news_url("https://example.com/a") == "https://example.com/a"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/", "http://localhost/", "file:///etc/passwd", "http://10.0.0.5/x"])
def test_ssrf_guard(url):
    with pytest.raises(ValueError):
        src.assert_public_http_url(url)


def test_within_days_and_relevance():
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    assert src.within_days({"published_at": "2026-10-05T00:00:00+00:00"}, 7, now)
    assert not src.within_days({"published_at": "2026-09-01T00:00:00+00:00"}, 7, now)
    assert src.relevance("Pilbara lithium", {"title": "Pilbara lithium boom"}) == 1.0
    assert src.relevance("Pilbara lithium", {"title": "football"}) == 0.0

"""mining-news-mcp：矿业新闻聚合 MCP server。

工具：
  search(query, days=7, limit=10)   Google News RSS + GDELT 聚合检索，去重、按相关度排序
  fetch_article(url)                抓取正文（trafilatura），失败降级到 fixtures / RSS 摘要

联网失败或 MINING_OFFLINE=1 时使用 fixtures/news/ 下的真实 RSS 快照（data_source=fixture）。
运行：python -m servers.mining_news.server   （MCP_TRANSPORT=streamable-http 切到 HTTP，默认端口 8001）
"""
from __future__ import annotations

import json
import logging
from typing import Any

import trafilatura
from mcp.server.fastmcp.exceptions import ToolError

from servers.common import http_client, is_offline, make_server, now_iso, run_server, threaded
from servers.mining_news import sources as src

log = logging.getLogger("mining-news")

mcp = make_server(
    "mining-news",
    default_port=8001,
    instructions="矿业新闻检索与正文抓取。结果里的 url 可直接作为引用；data_source=fixture 表示离线快照。",
)

# search 结果缓存：fetch_article 抓正文失败时可以退回到 RSS 摘要
_SEEN: dict[str, dict] = {}


def _rank(query: str, items: list[dict], limit: int) -> list[dict]:
    for it in items:
        it["relevance"] = src.relevance(query, it)
    items = [it for it in src.dedupe(items) if it["relevance"] > 0]
    items.sort(key=lambda x: (x["relevance"], x.get("published_at") or ""), reverse=True)
    return items[:limit]


@mcp.tool()
@threaded
def search(query: str, days: int = 7, limit: int = 10) -> dict[str, Any]:
    """检索最近 N 天的矿业新闻。

    Args:
        query: 关键词，如 "Pilbara lithium"、"Pilgangoora"
        days: 回看天数 1–90，默认 7
        limit: 最多返回条数 1–50，默认 10
    Returns:
        {query, results: [{title, url, source, published_at, snippet, relevance, source_url, retrieved_at, data_source}], ...}
    """
    if not query or not query.strip():
        raise ToolError("query 不能为空")
    days = max(1, min(int(days), 90))
    limit = max(1, min(int(limit), 50))
    retrieved_at = now_iso()
    errors: list[str] = []
    items: list[dict] = []
    data_source = "live"

    if not is_offline():
        for name, fn in (("google-news", src.google_news), ("gdelt", src.gdelt)):
            # GDELT 限流很严（5 秒 1 次）：Google 已给出足够结果时不再请求，只在不足 / 失败时补充
            if name == "gdelt" and len(_rank(query, [dict(it) for it in items], limit)) >= limit:
                continue
            try:
                got = fn(query, days)
                items.extend(got)
                log.info("%s 返回 %d 条", name, len(got))
            except Exception as e:  # noqa: BLE001
                log.warning("%s 失败: %s", name, e)
                errors.append(f"{name}: {type(e).__name__}: {e}"[:160])
        items = [it for it in items if src.within_days(it, days)]

    ranked = _rank(query, items, limit)
    note = ""
    if not ranked:
        data_source = "fixture"
        fx, meta = src.load_fixture_items()
        windowed = [it for it in fx if src.within_days(it, days)]
        ranked = _rank(query, [dict(it) for it in windowed], limit)
        snap = ", ".join(sorted({v for v in meta.values() if v}))
        note = f"使用离线快照（{snap}）"
        if not ranked:  # 快照比时间窗旧：放宽时间窗，并在 note 里说明
            ranked = _rank(query, [dict(it) for it in fx], limit)
            note += f"；快照早于 {days} 天时间窗，已放宽"
        if errors:
            note += "；联网失败：" + " | ".join(errors)

    for it in ranked:
        it.update(source_url=it["url"], retrieved_at=retrieved_at, data_source=data_source)
        _SEEN[it["url"]] = it
    return {
        "query": query,
        "days": days,
        "count": len(ranked),
        "results": ranked,
        "data_source": data_source,
        "retrieved_at": retrieved_at,
        "note": note,
    }


def _extract(html: str, url: str) -> dict | None:
    out = trafilatura.extract(html, url=url, output_format="json", with_metadata=True, include_comments=False)
    if not out:
        return None
    d = json.loads(out)
    return d if (d.get("text") or "").strip() else None


@mcp.tool()
@threaded
def fetch_article(url: str, max_chars: int = 6000) -> dict[str, Any]:
    """抓取新闻正文。

    Args:
        url: search 返回的 url（Google News 跳转链接会自动解析成原文链接）
        max_chars: 正文最多保留字符数，默认 6000
    Returns:
        {url, resolved_url, title, text, text_source(full|snippet), published_at, source, source_url, retrieved_at, data_source}
    """
    if not url.startswith(("http://", "https://")):
        raise ToolError("url 必须是 http(s) 链接")
    retrieved_at = now_iso()
    known = _SEEN.get(url) or {}
    err = ""

    if not is_offline():
        try:
            real = src.resolve_google_news_url(url)
            src.assert_public_http_url(real)
            with http_client(headers={"User-Agent": "Mozilla/5.0"}) as cli:
                r = cli.get(real)
            r.raise_for_status()
            d = _extract(r.text, str(r.url))
            if d:
                return {
                    "url": url,
                    "resolved_url": str(r.url),
                    "title": d.get("title") or known.get("title", ""),
                    "text": d["text"][:max_chars],
                    "text_source": "full",
                    "published_at": d.get("date") or known.get("published_at"),
                    "source": d.get("sitename") or known.get("source", ""),
                    "source_url": str(r.url),
                    "retrieved_at": retrieved_at,
                    "data_source": "live",
                }
            err = "正文提取为空"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:160]
            log.warning("fetch_article 失败 %s: %s", url, err)

    fx = src.load_fixture_article(url)
    base = fx or known
    if not base:
        raise ToolError(f"无法获取正文（{err or '离线模式且无快照'}）：{url}")
    text = (base.get("text") or base.get("snippet") or "").strip() or base.get("title", "")
    return {
        "url": url,
        "resolved_url": base.get("resolved_url", url),
        "title": base.get("title", ""),
        "text": text[:max_chars],
        "text_source": "full" if base.get("text") else "snippet",
        "published_at": base.get("published_at"),
        "source": base.get("source", ""),
        "source_url": url,
        "retrieved_at": retrieved_at,
        "data_source": "fixture" if fx else "live",
        "note": "正文抓取失败，退回快照/摘要" + (f"（{err}）" if err else ""),
    }


def main() -> None:
    run_server(mcp)


if __name__ == "__main__":
    main()

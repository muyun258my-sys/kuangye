"""新闻源：Google News RSS、GDELT DOC API（都免 key），以及 fixtures 快照兜底。"""
from __future__ import annotations

import base64
import email.utils
import html
import ipaddress
import json
import logging
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import feedparser

from servers.common import FIXTURES, http_client

log = logging.getLogger("mining-news")

NEWS_FIXTURES = FIXTURES / "news"
GOOGLE_RSS = "https://news.google.com/rss/search?q={q}+when:{days}d&hl=en-AU&gl=AU&ceid=AU:en"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"

STOPWORDS = {"the", "a", "an", "of", "and", "or", "in", "on", "for", "to", "news", "today", "今日", "简报"}


# ---------------------------------------------------------------- 工具函数
def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat() if dt else None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        pass
    try:  # GDELT: 20261008T053000Z
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+|[一-鿿]+", (text or "").lower()) if t not in STOPWORDS]


def relevance(query: str, item: dict) -> float:
    """非常朴素的相关度：查询词在标题里命中记 2 分，摘要里命中记 1 分，按词数归一。"""
    q = set(tokens(query))
    if not q:
        return 0.0
    title = set(tokens(item.get("title", "")))
    body = set(tokens(item.get("snippet", "")))
    score = sum(2 if t in title else 1 if t in body else 0 for t in q)
    return round(score / (2 * len(q)), 3)


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"\s+-\s+[^-]+$", "", t.lower())).strip()


def dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        k = norm_title(it["title"]) or it["url"]
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def assert_public_http_url(url: str) -> None:
    """fetch_article 只抓公网 http(s)，避免被当成 SSRF 跳板。"""
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.hostname:
        raise ValueError(f"只支持 http(s) URL：{url}")
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"无法解析域名 {p.hostname}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"拒绝访问内网地址 {p.hostname} -> {ip}")


# ---------------------------------------------------------------- Google News
def parse_google_rss(xml: bytes | str) -> list[dict]:
    feed = feedparser.parse(xml)
    out = []
    for e in feed.entries:
        title = _strip_html(e.get("title", ""))
        src = e.get("source", {}) or {}
        source = src.get("title") or (title.rsplit(" - ", 1)[1] if " - " in title else "")
        clean_title = title.rsplit(" - ", 1)[0] if source and title.endswith(" - " + source) else title
        snippet = _strip_html(e.get("summary", ""))
        if snippet.startswith(clean_title):  # Google 的 summary 通常只是 "标题 + 来源"
            snippet = snippet[len(clean_title):].strip(" - ") or ""
            if snippet == source:
                snippet = ""
        out.append(
            {
                "title": clean_title,
                "url": e.get("link", ""),
                "source": source,
                "source_domain": urlparse(src.get("href", "")).hostname or "",
                "published_at": _iso(_parse_dt(e.get("published"))),
                "snippet": snippet,
                "provider": "google-news",
            }
        )
    return out


def google_news(query: str, days: int) -> list[dict]:
    url = GOOGLE_RSS.format(q=quote_plus(query), days=days)
    with http_client(headers={"User-Agent": "Mozilla/5.0"}) as cli:
        r = cli.get(url)
    r.raise_for_status()
    return parse_google_rss(r.content)


def resolve_google_news_url(url: str) -> str:
    """Google News 的 /rss/articles/<id> 是跳转页。尽力解析出原文 URL，失败就原样返回。

    1) 老格式：id 是 base64 protobuf，里面直接含原文 URL
    2) 新格式：先取页面上的 data-n-a-sg / data-n-a-ts，再调 batchexecute 换真实 URL
    """
    p = urlparse(url)
    if p.hostname != "news.google.com" or "/articles/" not in p.path:
        return url
    art_id = p.path.rsplit("/", 1)[-1]
    try:
        raw = base64.urlsafe_b64decode(art_id + "=" * (-len(art_id) % 4))
        m = re.search(rb"https?://[\x21-\x7e]+", raw)
        if m:
            return m.group(0).decode()
    except (ValueError, TypeError):
        pass
    try:
        with http_client(headers={"User-Agent": "Mozilla/5.0"}) as cli:
            page = cli.get(f"https://news.google.com/articles/{art_id}").text
            sg = re.search(r'data-n-a-sg="([^"]+)"', page)
            ts = re.search(r'data-n-a-ts="([^"]+)"', page)
            if not (sg and ts):
                return url
            payload = [
                "Fbv4je",
                json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], art_id, int(ts.group(1)), sg.group(1)]),
            ]
            r = cli.post(
                "https://news.google.com/_/DotsSplashUi/data/batchexecute",
                data={"f.req": json.dumps([[payload]])},
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            )
            body = r.text.split("\n\n", 1)[-1]
            inner = json.loads(json.loads(body)[0][2])
            return inner[1] if isinstance(inner, list) and len(inner) > 1 else url
    except Exception as e:  # noqa: BLE001
        log.info("Google News 链接解析失败（使用原链接）: %s", e)
        return url


# ---------------------------------------------------------------- GDELT
def gdelt(query: str, days: int, max_records: int = 25) -> list[dict]:
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": max_records,
        "timespan": f"{max(1, days)}d",
        "sort": "datedesc",
    }
    with http_client() as cli:
        r = cli.get(GDELT, params=params)
    r.raise_for_status()
    try:
        arts = r.json().get("articles", [])
    except json.JSONDecodeError as e:  # 限流时返回的是纯文本提示
        raise RuntimeError("GDELT 返回非 JSON：" + r.text[:80]) from e
    return [
        {
            "title": _strip_html(a.get("title", "")),
            "url": a.get("url", ""),
            "source": a.get("domain", ""),
            "source_domain": a.get("domain", ""),
            "published_at": _iso(_parse_dt(a.get("seendate"))),
            "snippet": "",
            "provider": "gdelt",
        }
        for a in arts
        if a.get("url")
    ]


# ---------------------------------------------------------------- fixtures
def load_fixture_items() -> tuple[list[dict], dict]:
    """读取 fixtures/news/*.json 快照。返回 (items, meta)。"""
    items, meta = [], {}
    for p in sorted(Path(NEWS_FIXTURES).glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        items.extend(data.get("items", []))
        meta[p.name] = data.get("snapshot_at")
    return items, meta


def load_fixture_article(url: str) -> dict | None:
    arts = Path(NEWS_FIXTURES) / "articles.json"
    if arts.exists():
        data = json.loads(arts.read_text(encoding="utf-8"))
        if url in data:
            return data[url]
    for it in load_fixture_items()[0]:
        if it["url"] == url:
            return it
    return None


def within_days(item: dict, days: int, now: datetime | None = None) -> bool:
    dt = _parse_dt(item.get("published_at"))
    if dt is None:
        return True
    now = now or datetime.now(timezone.utc)
    return dt >= now - timedelta(days=days)

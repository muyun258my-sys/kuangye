"""刷新离线新闻快照 fixtures/news/snapshot.json（真实 Google News RSS 数据）。

用法：
  uv run python scripts/snapshot_news.py                 # 联网抓取
  uv run python scripts/snapshot_news.py --from-dir DIR  # 解析已下载的 RSS xml（文件名=查询词.xml）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from servers.common import FIXTURES, now_iso  # noqa: E402
from servers.mining_news import sources as src  # noqa: E402

QUERIES = ["Pilbara lithium", "Pilgangoora", "Wodgina", "PLS Group lithium", "Mineral Resources lithium", "spodumene price"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-dir")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default=str(FIXTURES / "news" / "snapshot.json"))
    a = ap.parse_args()

    items: list[dict] = []
    if a.from_dir:
        for p in sorted(Path(a.from_dir).glob("*.xml")):
            got = src.parse_google_rss(p.read_bytes())
            print(f"{p.name}: {len(got)} 条")
            items.extend(got)
        snapshot_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(p.stat().st_mtime for p in Path(a.from_dir).glob("*.xml"))))
    else:
        for q in QUERIES:
            got = src.google_news(q, a.days)
            print(f"{q}: {len(got)} 条")
            items.extend(got)
            time.sleep(1)
        snapshot_at = now_iso()

    items = src.dedupe(items)
    items.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "snapshot_at": snapshot_at,
                "provider": "google-news-rss",
                "queries": QUERIES,
                "note": "真实 Google News RSS 快照，仅含标题/来源/时间/链接；离线模式下作为兜底数据。",
                "items": items,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"写入 {out}：{len(items)} 条（去重后）")


if __name__ == "__main__":
    main()

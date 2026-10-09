"""mineral-pdf-mcp：技术报告 PDF 资源量解析 MCP server。

工具：
  extract_resources(pdf_url)   解析 PDF 中的 Mineral Resource 表（JORC / NI 43-101）
  list_reports()               列出已登记的项目报告（fixtures/reports/index.json）

pdf_url 支持：
  - http(s)://...      远程下载（失败时若在 index.json 登记过，回退到本地样例）
  - fixture://<项目>   直接用仓库里的样例 PDF，如 fixture://pilgangoora
  - 本地路径 / file://  仅限 fixtures/ 或 MINING_PDF_DIR 下的文件
运行：python -m servers.mineral_pdf.server   （MCP_TRANSPORT=streamable-http 切到 HTTP，默认端口 8002）
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from mcp.server.fastmcp.exceptions import ToolError

from servers.common import FIXTURES, ROOT, http_client, is_offline, make_server, now_iso, run_server, threaded
from servers.mineral_pdf.parser import parse_pdf

log = logging.getLogger("mineral-pdf")

REPORTS = FIXTURES / "reports"
CACHE = Path(os.environ.get("MINING_CACHE_DIR", ROOT / ".cache")) / "pdf"
MAX_BYTES = 40 * 1024 * 1024
ALLOWED_DIRS = [REPORTS.resolve()] + [Path(p).resolve() for p in os.environ.get("MINING_PDF_DIR", "").split(os.pathsep) if p]

mcp = make_server(
    "mineral-pdf",
    default_port=8002,
    instructions="从矿业技术报告 PDF 中抽取资源量表（Measured/Indicated/Inferred）。data_source=fixture 表示仓库内的示意样例 PDF。",
)


def _index() -> dict:
    p = REPORTS / "index.json"
    return json.loads(p.read_text(encoding="utf-8")).get("projects", {}) if p.exists() else {}


def _lookup(key: str) -> tuple[str, dict] | None:
    """按项目名 / 别名 / 登记的 pdf_url 查 index。"""
    k = key.strip().lower()
    for name, meta in _index().items():
        if k == name.lower() or k in meta.get("aliases", []) or (meta.get("pdf_url") and meta["pdf_url"] == key):
            return name, meta
    return None


def _local(path: Path) -> bytes:
    rp = path.resolve()
    if not any(rp.is_relative_to(d) for d in ALLOWED_DIRS):
        raise ToolError(f"出于安全考虑只允许读取 fixtures/reports 或 MINING_PDF_DIR 下的文件：{path}")
    if not rp.exists():
        raise ToolError(f"文件不存在：{path}")
    return rp.read_bytes()


def _download(url: str) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    cp = CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".pdf")
    if cp.exists():
        return cp.read_bytes()
    from servers.mining_news.sources import assert_public_http_url

    assert_public_http_url(url)
    with http_client(timeout=30) as cli, cli.stream("GET", url) as r:
        r.raise_for_status()
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > MAX_BYTES:
                raise ValueError("PDF 超过 40MB 上限")
    data = bytes(buf)
    if not data.startswith(b"%PDF"):
        raise ValueError("下载内容不是 PDF（可能是登录页 / 反爬页面）")
    cp.write_bytes(data)
    return data


def _load(pdf_url: str) -> tuple[bytes, str, str, str | None, str]:
    """返回 (pdf_bytes, data_source, 实际来源, 项目名提示, note)。"""
    u = pdf_url.strip()
    if u.startswith("fixture://"):
        hit = _lookup(u[len("fixture://"):])
        if not hit:
            raise ToolError(f"未登记的样例：{u}；可用：{', '.join('fixture://' + n.lower() for n in _index())}")
        name, meta = hit
        return _local(REPORTS / meta["fixture"]), "fixture", f"fixture://{name.lower()}", name, "仓库内样例 PDF（示意数据）"
    if u.startswith(("http://", "https://")):
        hit = _lookup(u)
        err = "离线模式"
        if not is_offline():
            try:
                return _download(u), "live", u, hit[0] if hit else None, ""
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:160]
                log.warning("下载失败 %s: %s", u, err)
        if hit:
            name, meta = hit
            return _local(REPORTS / meta["fixture"]), "fixture", f"fixture://{name.lower()}", name, f"远程 PDF 不可用（{err}），已回退到样例 PDF"
        raise ToolError(f"无法下载 PDF（{err}）：{u}")
    # url2pathname：Windows 上 file:///C:/x.pdf → C:\x.pdf（直接取 urlparse().path 会得到 /C:/x.pdf）
    path = Path(url2pathname(urlparse(u).path)) if u.startswith("file://") else Path(u)
    if not path.is_absolute():
        path = (ROOT / path) if (ROOT / path).exists() else (REPORTS / path)
    return _local(path), "fixture", str(path), None, ""


@mcp.tool()
def list_reports() -> dict[str, Any]:
    """列出已登记的项目技术报告（项目名、运营方、pdf_url、样例文件）。"""
    return {
        "projects": [
            {
                "project": n,
                "aliases": m.get("aliases", []),
                "operator": m.get("operator"),
                "region": m.get("region"),
                "commodity": m.get("commodity"),
                "pdf_url": m.get("pdf_url"),
                "fixture_url": f"fixture://{n.lower()}",
            }
            for n, m in _index().items()
        ],
        "retrieved_at": now_iso(),
    }


@mcp.tool()
@threaded
def extract_resources(pdf_url: str) -> dict[str, Any]:
    """解析技术报告 PDF 的资源量表。

    Args:
        pdf_url: PDF 链接（http/https）、fixture://<项目名>，或 fixtures/reports 下的文件路径
    Returns:
        {project, commodity, reporting_code (JORC | NI 43-101), cutoff,
         categories: [{category, tonnage_mt, grade, unit, contained, contained_unit}],
         page, method (table|text), confidence, source_url, retrieved_at, data_source, warnings}
    """
    if not pdf_url or not pdf_url.strip():
        raise ToolError("pdf_url 不能为空")
    data, data_source, actual, hint, note = _load(pdf_url)
    try:
        r = parse_pdf(data, project_hint=hint)
    except Exception as e:  # noqa: BLE001 —— 损坏的 PDF 等
        raise ToolError(f"PDF 解析失败：{type(e).__name__}: {e}") from e
    if not r.categories:
        raise ToolError("未在 PDF 中找到资源量表（Measured / Indicated / Inferred）：" + "; ".join(r.warnings))
    out = r.as_dict()
    out.update(
        source_url=pdf_url if data_source == "live" else actual,
        requested_url=pdf_url,
        retrieved_at=now_iso(),
        data_source=data_source,
        note=note,
    )
    return out


def main() -> None:
    run_server(mcp)


if __name__ == "__main__":
    main()

import io

import httpx
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as cv
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

from helpers import call, mcp_session
from servers.common import FIXTURES
from servers.mineral_pdf import parser
from servers.mineral_pdf import server as pdf_server
from servers.mineral_pdf.server import mcp

PIL = (FIXTURES / "reports" / "pilgangoora_sample.pdf").read_bytes()


def text_pdf(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    c = cv.Canvas(buf, pagesize=A4)
    y = 800
    for ln in lines:
        c.setFont("Courier", 10)
        c.drawString(40, y, ln)
        y -= 14
    c.showPage()
    c.save()
    return buf.getvalue()


def table_pdf(rows: list[list[str]], intro: str = "") -> bytes:
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph

    buf = io.BytesIO()
    t = Table(rows)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, "black")]))
    SimpleDocTemplate(buf, pagesize=A4).build([Paragraph(intro, getSampleStyleSheet()["BodyText"]), t])
    return buf.getvalue()


# ------------------------------------------------------------------ MCP 工具
async def test_list_tools():
    async with mcp_session(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert {"extract_resources", "list_reports"} <= names


async def test_pilgangoora_table_path():
    async with mcp_session(mcp) as c:
        err, r = await call(c, "extract_resources", {"pdf_url": "fixture://pilgangoora"})
    assert not err, r
    assert (r["project"], r["commodity"], r["reporting_code"], r["cutoff"]) == ("Pilgangoora", "Li2O", "JORC", "0.2% Li2O")
    assert r["method"] == "table" and r["page"] == 2 and r["confidence"] >= 0.9
    cats = {c["category"]: c for c in r["categories"]}
    assert set(cats) == {"Measured", "Indicated", "Inferred", "Total"}  # 没把 Proved/Probable 混进来
    assert cats["Indicated"] == pytest.approx(
        {"category": "Indicated", "tonnage_mt": 251.6, "grade": 1.18, "unit": "% Li2O", "contained": 2969.0, "contained_unit": "kt Li2O"}
    )
    assert r["data_source"] == "fixture" and r["source_url"] == "fixture://pilgangoora" and r["retrieved_at"]


async def test_wodgina_text_path_and_local_path():
    async with mcp_session(mcp) as c:
        _, a = await call(c, "extract_resources", {"pdf_url": "fixture://wodgina"})
        _, b = await call(c, "extract_resources", {"pdf_url": "fixtures/reports/wodgina_sample.pdf"})
    assert a["method"] == "text" and a["cutoff"] == "0.5% Li2O" and a["confidence"] >= 0.8
    total = next(c for c in a["categories"] if c["category"] == "Total")
    assert total["tonnage_mt"] == 257.9 and total["contained"] == 3003
    assert b["categories"] == a["categories"]


@pytest.mark.parametrize("url", ["", "fixture://nope", "pyproject.toml", "/etc/passwd", "../pyproject.toml"])
async def test_bad_inputs_are_structured_errors(url):
    async with mcp_session(mcp) as c:
        err, msg = await call(c, "extract_resources", {"pdf_url": url})
        ok_err, _ = await call(c, "list_reports")
    assert err and msg and not ok_err


def _patch_http(monkeypatch, handler, tmp_path):
    monkeypatch.setenv("MINING_OFFLINE", "0")
    monkeypatch.setattr(pdf_server, "CACHE", tmp_path)
    monkeypatch.setattr(pdf_server, "http_client", lambda **kw: httpx.Client(transport=httpx.MockTransport(handler)))
    import servers.mining_news.sources as s

    monkeypatch.setattr(s, "assert_public_http_url", lambda u: None)


async def test_live_download(monkeypatch, tmp_path):
    _patch_http(monkeypatch, lambda req: httpx.Response(200, content=PIL, headers={"content-type": "application/pdf"}), tmp_path)
    url = "https://example.com/asx/pilgangoora-mre.pdf"
    async with mcp_session(mcp) as c:
        err, r = await call(c, "extract_resources", {"pdf_url": url})
    assert not err, r
    assert r["data_source"] == "live" and r["source_url"] == url
    assert len(list(tmp_path.glob("*.pdf"))) == 1  # 已缓存


async def test_live_failure_falls_back_to_registered_fixture(monkeypatch, tmp_path):
    url = "https://example.com/asx/wodgina.pdf"
    idx = pdf_server._index()
    idx["Wodgina"] = {**idx["Wodgina"], "pdf_url": url}
    monkeypatch.setattr(pdf_server, "_index", lambda: idx)
    _patch_http(monkeypatch, lambda req: httpx.Response(200, text="<html>login</html>"), tmp_path)
    async with mcp_session(mcp) as c:
        err, r = await call(c, "extract_resources", {"pdf_url": url})
        err2, msg = await call(c, "extract_resources", {"pdf_url": "https://example.com/unregistered.pdf"})
    assert not err and r["data_source"] == "fixture" and "回退" in r["note"] and "不是 PDF" in r["note"]
    assert r["project"] == "Wodgina"
    assert err2 and "无法下载" in msg


async def test_corrupt_pdf(monkeypatch, tmp_path):
    _patch_http(monkeypatch, lambda req: httpx.Response(200, content=b"%PDF-1.4 garbage"), tmp_path)
    async with mcp_session(mcp) as c:
        err, msg = await call(c, "extract_resources", {"pdf_url": "https://example.com/bad.pdf"})
    assert err and "解析失败" in msg


# ------------------------------------------------------------------ 解析器单元测试
def test_ni43101_text_without_total_derives_total():
    pdf = text_pdf(
        [
            "Lac Example Lithium Project - Technical Report (NI 43-101)",
            "Prepared under National Instrument 43-101. Cut-off grade of 0.6% Li2O.",
            "Inferred Resources are not converted to reserves and carry lower confidence.",  # 正文句子，不能当表格行
            "Category       Tonnes (Mt)   Li2O (%)",
            "Indicated          40.0        1.40",
            "Inferred           10.0        1.00",
        ]
    )
    r = parser.parse_pdf(pdf)
    assert r.reporting_code == "NI 43-101" and r.cutoff == "0.6% Li2O" and r.project == "Lac Example"
    cats = {c["category"]: c for c in r.categories}
    assert set(cats) == {"Indicated", "Inferred", "Total"}
    assert cats["Indicated"]["contained"] == 560.0 and cats["Indicated"]["contained_derived"]
    assert cats["Total"]["tonnage_mt"] == 50.0 and cats["Total"]["grade"] == pytest.approx(1.32) and cats["Total"]["derived"]
    assert any("Total" in w for w in r.warnings)


def test_reserve_only_document_is_not_a_resource():
    pdf = text_pdf(["Ore Reserve Statement (JORC)", "Category  Tonnes (Mt)  Li2O (%)", "Proved  10.0  1.2", "Probable  20.0  1.1", "Total  30.0  1.13"])
    r = parser.parse_pdf(pdf)
    assert r.categories == [] and r.warnings


def test_table_with_kilotonnes_and_inconsistent_total():
    pdf = table_pdf(
        [
            ["Classification", "Tonnage ('000 t)", "Grade (% Li2O)", "Contained Li2O (kt)"],
            ["Indicated", "12,000", "1.50", "180"],
            ["Inferred", "8,000", "1.00", "80"],
            ["Total", "25,000", "1.30", "260"],
        ],
        intro="Example Project. Mineral Resource reported under the JORC Code.",
    )
    r = parser.parse_pdf(pdf)
    cats = {c["category"]: c for c in r.categories}
    assert r.method == "table" and cats["Indicated"]["tonnage_mt"] == 12.0
    assert any("不一致" in w for w in r.warnings)


def test_number_parsing():
    assert parser.num("2,969") == 2969 and parser.num("1.18 %") == 1.18 and parser.num("n/a") is None

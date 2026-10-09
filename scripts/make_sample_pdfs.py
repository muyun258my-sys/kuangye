"""生成 fixtures/reports/ 下的样例技术报告 PDF（离线兜底 + 解析器测试用）。

两份 PDF 刻意用不同版式，覆盖解析器的两条路径：
  pilgangoora_sample.pdf  带框线的表格  → pdfplumber.extract_tables 路径
  wodgina_sample.pdf      无框线的对齐文本 → 文本行解析路径

注意：数值为【示意数据】，量级参考公开披露但并非官方数字，PDF 页眉页脚均有标注。
运行：uv run python scripts/make_sample_pdfs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from servers.common import FIXTURES  # noqa: E402

OUT = FIXTURES / "reports"
DISCLAIMER = "SAMPLE - illustrative figures for software testing; not an official ASX/JORC disclosure."
ss = getSampleStyleSheet()


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica-Oblique", 7)
    canvas.setFillColor(colors.grey)
    canvas.drawString(15 * mm, 10 * mm, DISCLAIMER)
    canvas.drawRightString(195 * mm, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


def pilgangoora() -> Path:
    path = OUT / "pilgangoora_sample.pdf"
    doc = SimpleDocTemplate(str(path), pagesize=A4, title="Pilgangoora Mineral Resource Update (SAMPLE)")
    P = lambda t, s="BodyText": Paragraph(t, ss[s])  # noqa: E731
    story = [
        P("Pilgangoora Lithium-Tantalum Project", "Title"),
        P("Mineral Resource and Ore Reserve Statement - SAMPLE"),
        P(DISCLAIMER, "Italic"),
        Spacer(1, 8 * mm),
        P("1. Introduction", "Heading2"),
        P(
            "The Pilgangoora Project is located in the Pilbara region of Western Australia, approximately 120 km south of "
            "Port Hedland. This statement has been prepared in accordance with the 2012 Edition of the Australasian Code "
            "for Reporting of Exploration Results, Mineral Resources and Ore Reserves (the JORC Code)."
        ),
        P("2. Geology", "Heading2"),
        P("Mineralisation is hosted in swarms of spodumene-bearing pegmatite dykes intruding greenstone sequences."),
        PageBreak(),
        P("3. Mineral Resource Estimate", "Heading2"),
        P("The Mineral Resource is reported above a cut-off grade of 0.2% Li2O and is inclusive of Ore Reserves."),
        Spacer(1, 4 * mm),
    ]
    rows = [
        ["Category", "Tonnes (Mt)", "Li2O (%)", "Ta2O5 (ppm)", "Contained Li2O (kt)"],
        ["Measured", "22.1", "1.31", "120", "290"],
        ["Indicated", "251.6", "1.18", "105", "2,969"],
        ["Inferred", "140.3", "1.05", "98", "1,473"],
        ["Total", "414.0", "1.14", "103", "4,732"],
    ]
    t = Table(rows, colWidths=[35 * mm, 28 * mm, 25 * mm, 28 * mm, 40 * mm])
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ]
        )
    )
    story += [
        P("Table 1: Pilgangoora Mineral Resource Estimate (JORC 2012)"),
        t,
        Spacer(1, 6 * mm),
        P("Note: totals may not sum due to rounding. Contained Li2O = Tonnes x Li2O grade."),
        Spacer(1, 6 * mm),
        P("4. Ore Reserve Estimate", "Heading2"),
    ]
    rows2 = [
        ["Category", "Tonnes (Mt)", "Li2O (%)", "Contained Li2O (kt)"],
        ["Proved", "18.0", "1.30", "234"],
        ["Probable", "196.4", "1.20", "2,357"],
        ["Total", "214.4", "1.21", "2,591"],
    ]
    t2 = Table(rows2, colWidths=[35 * mm, 28 * mm, 25 * mm, 40 * mm])
    t2.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.black), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    story += [P("Table 2: Pilgangoora Ore Reserve (JORC 2012)"), t2]
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return path


def wodgina() -> Path:
    """无框线：用 canvas 逐行写对齐文本，模拟扫描/排版类报告。"""
    from reportlab.pdfgen import canvas as cv

    path = OUT / "wodgina_sample.pdf"
    c = cv.Canvas(str(path), pagesize=A4)
    c.setTitle("Wodgina Lithium Project Resource Update (SAMPLE)")
    W, H = A4

    def page(lines, start_y=H - 30 * mm, font=("Helvetica", 10), lead=14):
        y = start_y
        for ln in lines:
            if isinstance(ln, tuple):
                c.setFont(*ln[1])
                ln = ln[0]
            else:
                c.setFont(*font)
            c.drawString(20 * mm, y, ln)
            y -= lead
        c.setFont("Helvetica-Oblique", 7)
        c.drawString(15 * mm, 10 * mm, DISCLAIMER)
        c.showPage()

    page(
        [
            ("Wodgina Lithium Project", ("Helvetica-Bold", 18)),
            "",
            ("ASX Announcement - Mineral Resource Update (SAMPLE)", ("Helvetica", 12)),
            "",
            "The Wodgina Lithium Project is located in the Pilbara region of Western Australia,",
            "approximately 110 km south of Port Hedland. Estimates are reported in accordance with",
            "the JORC Code (2012 Edition). Competent Person statements are included in Appendix A.",
        ]
    )
    mono = ("Courier", 10)
    page(
        [
            ("Mineral Resource Statement", ("Helvetica-Bold", 13)),
            "",
            "Reported at a 0.5% Li2O cut-off grade, inclusive of Ore Reserves.",
            "",
            ("Resource Category      Tonnes (Mt)    Grade (% Li2O)    Contained Li2O (kt)", ("Courier-Bold", 10)),
            ("Measured                     25.1             1.28                  321", mono),
            ("Indicated                   148.6             1.17                1,739", mono),
            ("Inferred                     84.2             1.12                  943", mono),
            ("Total                       257.9             1.16                3,003", mono),
            "",
            "Figures are rounded; minor discrepancies may occur in totals.",
        ]
    )
    c.save()
    return path


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for fn in (pilgangoora, wodgina):
        print("写入", fn())

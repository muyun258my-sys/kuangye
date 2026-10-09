"""技术报告 PDF → 结构化资源量（Mineral Resource）。

流程：
  1) pdfplumber.extract_tables()：找表头含 吨位 + 品位 的表，按列名映射，识别 Measured / Indicated / Inferred / Total 行
  2) 表格失败 → 文本行解析：逐行匹配 "<类别> <数字> <数字> ... "
  3) 交叉校验（各类别之和≈Total、金属量≈吨位×品位）给出 confidence

只解析资源量；Proved / Probable（Ore Reserve）表会被识别并跳过，避免把储量当资源量。
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pdfplumber

RESOURCE_CATS = {"measured": "Measured", "indicated": "Indicated", "inferred": "Inferred", "total": "Total"}
RESERVE_WORDS = re.compile(r"\b(proved|proven|probable)\b", re.I)
CAT_RE = re.compile(r"^\s*(measured|indicated|inferred|total)\b", re.I)
NUM_RE = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])\d+(?:\.\d+)?")

COMMODITIES = [  # (正则, 规范名, 品位单位, 金属量单位)
    (re.compile(r"\bLi\s?2\s?O\b", re.I), "Li2O", "% Li2O", "kt Li2O"),
    (re.compile(r"\bCu\b|\bcopper\b", re.I), "Cu", "% Cu", "kt Cu"),
    (re.compile(r"\bNi\b|\bnickel\b", re.I), "Ni", "% Ni", "kt Ni"),
    (re.compile(r"\bFe\b|\biron ore\b", re.I), "Fe", "% Fe", "Mt Fe"),
    (re.compile(r"\bAu\b|\bgold\b", re.I), "Au", "g/t Au", "koz Au"),
]


@dataclass
class ParseResult:
    project: str | None = None
    commodity: str | None = None
    reporting_code: str | None = None
    cutoff: str | None = None
    categories: list[dict] = field(default_factory=list)
    page: int | None = None
    method: str | None = None
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def num(s: str | None) -> float | None:
    if s is None:
        return None
    m = NUM_RE.search(str(s))
    return float(m.group(0).replace(",", "")) if m else None


# ---------------------------------------------------------------- 文档级元数据
def detect_reporting_code(text: str) -> str | None:
    jorc = len(re.findall(r"\bJORC\b", text))
    ni = len(re.findall(r"\bNI\s?43-101\b|National Instrument 43-101", text, re.I))
    if not (jorc or ni):
        return None
    return "JORC" if jorc >= ni else "NI 43-101"


def detect_commodity(text: str) -> tuple[str | None, str | None, str | None]:
    best, hits = None, 0
    for rx, name, gu, cu in COMMODITIES:
        n = len(rx.findall(text))
        if n > hits:
            best, hits = (name, gu, cu), n
    return best or (None, None, None)


def detect_cutoff(text: str) -> str | None:
    pats = [
        r"cut-?\s?off(?:\s+grade)?(?:\s+of)?\s+(\d+(?:\.\d+)?)\s*%\s*(Li\s?2\s?O|Cu|Ni|Fe)",
        r"(\d+(?:\.\d+)?)\s*%\s*(Li\s?2\s?O|Cu|Ni|Fe)\s+cut-?\s?off",
        r"cut-?\s?off(?:\s+grade)?(?:\s+of)?\s+(\d+(?:\.\d+)?)\s*(g/t)\s*(Au)?",
    ]
    for p in pats:
        m = re.search(p, text, re.I)
        if m:
            g = [x for x in m.groups() if x]
            unit = g[1].replace(" ", "")
            return f"{g[0]}% {unit}" if unit != "g/t" else f"{g[0]} g/t Au"
    return None


def detect_project(text: str, hint: str | None = None) -> str | None:
    head = text[:1500]
    m = re.search(r"\b([A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)?)\s+(?:Lithium(?:-Tantalum)?\s+|Copper\s+|Gold\s+|Nickel\s+)?Project\b", head)
    if m:
        name = re.sub(r"\s+(Lithium|Copper|Gold|Nickel)$", "", m.group(1))
        if name.lower() not in {"the", "this"}:
            return name
    return hint


# ---------------------------------------------------------------- 表格路径
def _header_map(header: list[str | None]) -> dict[str, int] | None:
    cols: dict[str, int] = {}
    for i, h in enumerate(header):
        h = (h or "").replace("\n", " ").strip().lower()
        if not h:
            continue
        if "categor" in h or "class" in h:
            cols.setdefault("category", i)
        elif "contain" in h or "metal" in h or "(kt)" in h or "koz" in h:
            cols.setdefault("contained", i)
        elif "tonn" in h or h in {"mt", "tonnes"} or "(mt)" in h:
            cols.setdefault("tonnage", i)
            if "kt" in h or "000" in h:
                cols["_tonnage_div"] = 1000
        elif "grade" in h or "%" in h or "g/t" in h:
            cols.setdefault("grade", i)
    if "tonnage" in cols and "grade" in cols:
        cols.setdefault("category", 0)
        return cols
    return None


def _from_table(tbl: list[list[str | None]]) -> list[dict] | None:
    if not tbl or len(tbl) < 2:
        return None
    flat = " ".join(" ".join(c or "" for c in r) for r in tbl)
    if RESERVE_WORDS.search(flat) and not re.search(r"\b(measured|indicated|inferred)\b", flat, re.I):
        return None  # Ore Reserve 表，跳过
    for hi in range(min(3, len(tbl))):  # 表头可能不在第一行
        cols = _header_map(tbl[hi])
        if not cols:
            continue
        out = []
        for row in tbl[hi + 1:]:
            cell = (row[cols["category"]] or "") if cols["category"] < len(row) else ""
            m = CAT_RE.match(cell)
            if not m:
                continue
            t = num(row[cols["tonnage"]]) if cols["tonnage"] < len(row) else None
            g = num(row[cols["grade"]]) if cols["grade"] < len(row) else None
            c = num(row[cols["contained"]]) if "contained" in cols and cols["contained"] < len(row) else None
            if t is None or g is None:
                continue
            out.append({"category": RESOURCE_CATS[m.group(1).lower()], "tonnage_mt": t / cols.get("_tonnage_div", 1), "grade": g, "contained": c})
        if out:
            return out
    return None


# ---------------------------------------------------------------- 文本路径
def _from_text(page_text: str) -> list[dict] | None:
    lines = page_text.splitlines()
    out = []
    for i, ln in enumerate(lines):
        m = CAT_RE.match(ln)
        if not m:
            continue
        # 避免把正文里 "Inferred Resources are ..." 这种句子当成表格行：要求后面至少两个数字
        nums = [float(x.replace(",", "")) for x in NUM_RE.findall(ln[m.end():])]
        if len(nums) < 2:
            continue
        # 往上找最近的表头；若是 Ore Reserve 段落就跳过
        ctx = " ".join(lines[max(0, i - 8):i]).lower()
        if RESERVE_WORDS.search(ctx) and "resource" not in ctx:
            continue
        t, g = nums[0], nums[1]
        c = None
        if len(nums) >= 3:  # 金属量通常在最后一列；用 吨位×品位 校验
            cand = nums[-1]
            expect = t * g * 10
            c = cand if expect and abs(cand - expect) / expect < 0.1 else None
        out.append({"category": RESOURCE_CATS[m.group(1).lower()], "tonnage_mt": t, "grade": g, "contained": c})
    return out or None


# ---------------------------------------------------------------- 校验 & 打分
def _finalize(cats: list[dict], commodity: str | None, grade_unit: str | None, contained_unit: str | None) -> tuple[list[dict], list[str], int]:
    warnings, checks = [], 0
    seen, uniq = set(), []
    for c in cats:  # 同一类别只保留第一次出现
        if c["category"] not in seen:
            seen.add(c["category"])
            uniq.append(c)
    for c in uniq:
        c["unit"] = grade_unit or "%"
        c["contained_unit"] = contained_unit or "kt"
        expect = round(c["tonnage_mt"] * c["grade"] * 10, 1) if (grade_unit or "%").startswith("%") else None
        if c["contained"] is None and expect is not None:
            c["contained"] = expect
            c["contained_derived"] = True
        elif expect and c["contained"] and abs(c["contained"] - expect) / expect < 0.05:
            checks += 1
    parts = [c for c in uniq if c["category"] != "Total"]
    total = next((c for c in uniq if c["category"] == "Total"), None)
    if total and parts:
        s = sum(c["tonnage_mt"] for c in parts)
        if abs(s - total["tonnage_mt"]) / total["tonnage_mt"] <= 0.02:
            checks += 2
        else:
            warnings.append(f"各类别吨位之和 {s:.1f} Mt 与 Total {total['tonnage_mt']} Mt 不一致")
    elif parts:
        s = sum(c["tonnage_mt"] for c in parts)
        uniq.append(
            {
                "category": "Total",
                "tonnage_mt": round(s, 2),
                "grade": round(sum(c["tonnage_mt"] * c["grade"] for c in parts) / s, 3) if s else 0,
                "unit": grade_unit or "%",
                "contained": round(sum(c["contained"] or 0 for c in parts), 1),
                "contained_unit": contained_unit or "kt",
                "derived": True,
            }
        )
        warnings.append("报告中未找到 Total 行，已按各类别加总")
    return uniq, warnings, checks


def parse_pdf(data: bytes, project_hint: str | None = None) -> ParseResult:
    res = ParseResult()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        texts = [(p.extract_text() or "") for p in pdf.pages]
        full = "\n".join(texts)
        res.reporting_code = detect_reporting_code(full)
        res.commodity, gu, cu = detect_commodity(full)
        res.cutoff = detect_cutoff(full)
        res.project = detect_project(full, project_hint)

        found = None
        for i, page in enumerate(pdf.pages):
            for tbl in page.extract_tables() or []:
                cats = _from_table(tbl)
                if cats:
                    found = (cats, i + 1, "table")
                    break
            if found:
                break
        if not found:
            scored = []
            for i, t in enumerate(texts):
                cats = _from_text(t)
                if cats:
                    bonus = 1 if re.search(r"mineral resource", t, re.I) else 0
                    scored.append((len(cats) + bonus, i + 1, cats))
            if scored:
                _, pg, cats = max(scored, key=lambda x: x[0])
                found = (cats, pg, "text")

    if not found:
        res.warnings.append("未找到资源量表（Measured / Indicated / Inferred）")
        return res

    cats, res.page, res.method = found
    res.categories, warns, checks = _finalize(cats, res.commodity, gu, cu)
    res.warnings.extend(warns)
    conf = 0.55 if res.method == "table" else 0.45
    conf += 0.08 * min(checks, 4)  # 金属量校验 + 加总校验
    conf += 0.04 * sum(x is not None for x in (res.reporting_code, res.cutoff, res.commodity))
    conf += 0.03 * min(3, len({c["category"] for c in res.categories} - {"Total"}))
    res.confidence = round(min(conf, 0.99), 2)
    return res

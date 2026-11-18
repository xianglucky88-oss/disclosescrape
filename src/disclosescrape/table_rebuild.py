"""单一职责：披露表格重建（table_rebuild v2）。

把 ann_page 排序/标注过的页内网格重建为边界确定的逻辑表。所有规则均为确定性
规则（无随机、无启发式打分），同一输入永远得到同一表格边界与落库内容：

- 合并单元格按 rowspan/colspan 展开，被覆盖槽位标记 covered；跨度越界或重叠即坏表。
- 独占一行的“单位：X”识别为表级单位（不进表头/数据）；表头内“（单位）”识别为列单位。
- 表头 = 首个含数值/空值单元格的行之前的所有行；多行表头逐列去重拼接为列名。
- 续页表头重印：续页表头与当前打开表表头末尾 K 行完全一致 → 视为重印，丢弃后按列追加。
- 跨页拼接：无表头且列数一致 → 追加；表头不匹配或列数不同 → 拒绝拼接，关闭当前表、
  另起新表。每次拼接/拒绝的依据写入该表 stitch_log，explain_stitching() 可导出说明。
- 坏表（非网格/非法单元格/跨度重叠）隔离进 quarantine：不关闭当前打开表、不污染同公告
  其他页与其他公告；修复后对该公告重跑本阶段即可幂等重试（upsert 按键覆盖）。
- 空字符串 = 布局空白(kind=blank)，JSON null = 真实缺失(kind=null)，“—” = 披露占位
  (kind=dash)，三者分开标记；短行右侧补 null 并记 missing_cols（列缺失）；超宽行坏表。
- 数值支持千分位、负号/Unicode 负号、括号负数、% 后缀；单元格保留 raw/num/unit/scale，
  换算 v = num × scale，可反查原文；prov（公告/来源/页码/页序/行/列）随单元格落库。
"""
from __future__ import annotations

import json
import re

from .ann_page import order_pages

VERSION = "v2"

# 单位 → 换算到基准单位（元 / % 自身）的倍率；未知单位 scale=1 并标 unit_known=False。
UNIT_SCALE = {
    "元": 1,
    "千元": 1_000,
    "万元": 10_000,
    "百万元": 1_000_000,
    "亿元": 100_000_000,
    "%": 1,
}

_DASHES = {"—", "–", "--", "-"}
_NUM_RE = re.compile(r"^[（(]?[+\-−]?[\d,，]*\.?\d+[)）]?%?$")
_UNIT_ROW_RE = re.compile(r"^单位\s*[:：]\s*([^\s，,；;]+)")
_HEADER_UNIT_RE = re.compile(r"[（(]\s*([^（）()\s]+)\s*[)）]\s*$")
_FOOTNOTE_RE = re.compile(r"^\s*(注\s*[：:0-9*、]|备注\s*[：:]|\*)")


class TableCorrupt(ValueError):
    """单页表格损坏：隔离到 quarantine，不向流水线传播。"""


# ---------- 网格校验与合并单元格展开 ----------

def _grid(table):
    if not isinstance(table, list) or not table:
        raise TableCorrupt(f"table is not a non-empty grid (got {type(table).__name__})")
    rows = []
    for r, row in enumerate(table):
        if not isinstance(row, list) or not row:
            raise TableCorrupt(f"row {r} is not a non-empty list")
        rows.append([_cell(x, r, ci) for ci, x in enumerate(row)])
    return rows


def _cell(raw, r, ci):
    if isinstance(raw, dict):
        text = raw.get("text")
        rs, cs = raw.get("rowspan", 1), raw.get("colspan", 1)
        if text is not None and not isinstance(text, (str, int, float)):
            raise TableCorrupt(f"cell({r},{ci}): unsupported text type {type(text).__name__}")
        bad_span = (isinstance(rs, bool) or isinstance(cs, bool)
                    or not isinstance(rs, int) or not isinstance(cs, int) or rs < 1 or cs < 1)
        if bad_span:
            raise TableCorrupt(f"cell({r},{ci}): bad span {rs!r}x{cs!r}")
        return {"text": text, "rowspan": rs, "colspan": cs, "r": r, "ci": ci}
    if raw is None or isinstance(raw, (str, int, float)):
        return {"text": raw, "rowspan": 1, "colspan": 1, "r": r, "ci": ci}
    raise TableCorrupt(f"cell({r},{ci}): unsupported cell type {type(raw).__name__}")


def _expand(rows):
    """展开合并单元格；返回 (grid, width)。槽位记录原始 (行, 行内单元格序号) 供 prov。"""
    width = max(sum(c["colspan"] for c in row) for row in rows)
    grid = [[None] * width for _ in rows]
    for r, row in enumerate(rows):
        c = 0
        for cell in row:
            while c < width and grid[r][c] is not None:
                c += 1
            if c + cell["colspan"] > width or r + cell["rowspan"] > len(rows):
                raise TableCorrupt(f"cell({cell['r']},{cell['ci']}): span overflows grid")
            for dr in range(cell["rowspan"]):
                for dc in range(cell["colspan"]):
                    if grid[r + dr][c + dc] is not None:
                        raise TableCorrupt(f"cell({cell['r']},{cell['ci']}): overlapping span")
                    grid[r + dr][c + dc] = {
                        "text": cell["text"], "r": cell["r"], "ci": cell["ci"],
                        "covered": (dr, dc) != (0, 0), "pad": False,
                    }
            c += cell["colspan"]
    for r in range(len(grid)):  # 短行右侧补 null（列缺失），与布局空白区分
        for c in range(width):
            if grid[r][c] is None:
                grid[r][c] = {"text": None, "r": r, "ci": None, "covered": False, "pad": True}
    return grid, width


# ---------- 单位行 / 表头 / 脚注识别 ----------

def _strip_unit_rows(grid):
    """消费开头的“单位：X”独占行，返回 (表级单位, 剩余网格)。"""
    unit, i = None, 0
    while i < len(grid):
        distinct = {c["text"].strip() for c in grid[i]
                    if isinstance(c["text"], str) and c["text"].strip()}
        if len(distinct) != 1:
            break
        m = _UNIT_ROW_RE.match(next(iter(distinct)))
        if not m:
            break
        if unit is None:
            unit = m.group(1)
        i += 1
    return unit, grid[i:]


def _is_valueish(cell):
    t = cell["text"]
    if t is None or isinstance(t, (int, float)):
        return True
    s = t.strip()
    return s == "" or s in _DASHES or bool(_NUM_RE.match(s))


def _split_header(grid):
    """表头 = 首个含数值/空值单元格（看第 2 列起）的行之前的所有行。"""
    for i, row in enumerate(grid):
        probe = row[1:] if len(row) > 1 else row
        if any(_is_valueish(c) for c in probe):
            return grid[:i], grid[i:]
    return grid, []


def _header_texts(header):
    return [["" if c["text"] is None else str(c["text"]).strip() for c in row]
            for row in header]


def _column_meta(header, width):
    """多行表头逐列去重拼接为列名；识别表头内“（单位）”为列单位。"""
    cols = []
    for c in range(width):
        parts, unit = [], None
        for row in header:
            t = row[c]["text"]
            s = t.strip() if isinstance(t, str) else ("" if t is None else str(t))
            if not s:
                continue
            m = _HEADER_UNIT_RE.search(s)
            if m and m.group(1) in UNIT_SCALE:
                unit = unit or m.group(1)
                s = _HEADER_UNIT_RE.sub("", s).strip()
            if s and s not in parts:
                parts.append(s)
        cols.append({"name": "/".join(parts) or f"col{c}", "unit": unit})
    return cols


def _is_footnote(row):
    t = row[0]["text"]
    if not isinstance(t, str) or not _FOOTNOTE_RE.match(t):
        return False
    return all(_parse(c["text"])[0] != "number" for c in row[1:])


# ---------- 数值解析：负号/括号/千分位/% ----------

def _parse(text):
    """→ (kind, 值, 是否百分数)。kind ∈ number/text/blank/null/dash。"""
    if text is None:
        return ("null", None, False)
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return ("number", float(text), False)
    s = str(text).strip()
    if not s:
        return ("blank", None, False)
    if s in _DASHES:
        return ("dash", None, False)
    t, neg = s, False
    if (t.startswith("(") and t.endswith(")")) or (t.startswith("（") and t.endswith("）")):
        neg, t = True, t[1:-1].strip()
    if t.startswith(("-", "−")):
        neg, t = True, t[1:].strip()
    elif t.startswith("+"):
        t = t[1:].strip()
    pct = t.endswith("%")
    if pct:
        t = t[:-1].strip()
    t = t.replace(",", "").replace("，", "")
    try:
        num = float(t)
    except ValueError:
        return ("text", s, False)
    return ("number", -num if neg else num, pct)


# ---------- 单元格与数据行重建 ----------

def _prov(page, gc):
    """单元格 provenance：ann_page 提供的来源与页序 + 原始网格行列，可反查原文。"""
    return {"ann": page.get("ann"), "src": page.get("src"), "page": page.get("page"),
            "order": page.get("order"), "row": gc["r"], "col": gc["ci"]}


def _value_cell(parsed, gc, col_unit, table_unit, page):
    kind, val, pct = parsed
    cell = {"kind": kind, "raw": gc["text"], "prov": _prov(page, gc)}
    if gc["covered"]:
        cell["covered"] = True
    if gc["pad"]:
        cell["missing_col"] = True
    if kind == "number":
        unit = "%" if pct else (col_unit or table_unit)
        scale = UNIT_SCALE.get(unit)
        cell["num"] = val
        cell["unit"] = unit
        cell["scale"] = scale if scale is not None else 1
        if scale is None:
            cell["unit_known"] = False
        cell["v"] = val * cell["scale"]
    elif kind == "text":
        cell["v"] = val
    else:
        cell["v"] = None
    return cell


def _process_body(table, body, page):
    for row in body:
        if _is_footnote(row):
            text = " ".join(dict.fromkeys(
                c["text"].strip() for c in row
                if isinstance(c["text"], str) and c["text"].strip()))
            table["footnotes"].append({"text": text, "prov": _prov(page, row[0])})
            continue
        cells, missing = [], 0
        for ci, gc in enumerate(row):
            cell = _value_cell(_parse(gc["text"]), gc,
                               table["columns"][ci]["unit"], table["unit"], page)
            if cell.get("missing_col"):
                missing += 1
            cells.append(cell)
        out = {"label": cells[0]["v"] if cells and cells[0]["kind"] == "text" else None,
               "page": page.get("page"), "cells": cells}
        if missing:
            out["missing_cols"] = missing
        table["rows"].append(out)


# ---------- 跨页拼接与表格边界 ----------

def _new_table(rec, idx, page, header, width, unit, reason):
    htexts = _header_texts(header)
    return {
        "table_id": f"{rec.get('id')}#t{idx}",
        "unit": unit,
        "columns": (_column_meta(header, width) if header
                    else [{"name": f"col{c}", "unit": None} for c in range(width)]),
        "header": htexts,
        "pages": [page.get("page")],
        "rows": [],
        "footnotes": [],
        "stitch_log": [{"page": page.get("page"), "action": "start", "reason": reason}],
        "_htexts": htexts,
        "_width": width,
    }


def _excerpt(table, limit=120):
    try:
        s = json.dumps(table, ensure_ascii=False)
    except (TypeError, ValueError):
        s = repr(table)
    return s[:limit]


def rebuild_announcement(rec):
    """重建单条公告 → {"tables", "quarantine", "_table_rebuild"}。纯函数、确定性。"""
    pages = rec.get("_ann_pages")
    if pages is None:  # 允许绕过 ann_page 单独调用
        pages = order_pages(rec.get("pages"), rec)
    tables, quarantine = [], []
    open_t = None

    def _close():
        nonlocal open_t
        if open_t is not None:
            open_t.pop("_htexts")
            open_t.pop("_width")
            tables.append(open_t)
            open_t = None

    for page in pages:
        pno = page.get("page")
        try:
            grid, width = _expand(_grid(page.get("table")))
        except TableCorrupt as exc:
            quarantine.append({"page": pno, "error": str(exc),
                               "excerpt": _excerpt(page.get("table"))})
            if open_t is not None:
                open_t["stitch_log"].append({
                    "page": pno, "action": "skip",
                    "reason": "corrupt page quarantined; open table kept for later pages"})
            continue
        unit, grid = _strip_unit_rows(grid)
        if not grid:
            continue
        header, body = _split_header(grid)
        if open_t is None:
            open_t = _new_table(rec, len(tables), page, header, width, unit,
                                "first table in announcement")
        else:
            htexts = _header_texts(header)
            k = len(htexts)
            if k and width == open_t["_width"] and htexts == open_t["_htexts"][-k:]:
                open_t["stitch_log"].append({
                    "page": pno, "action": "append",
                    "reason": f"reprinted header ({k} row(s)) dropped; {width} columns match"})
                open_t["pages"].append(pno)
            elif k:
                open_t["stitch_log"].append({
                    "page": pno, "action": "reject",
                    "reason": (f"incoming header {htexts} does not match open table "
                               f"header tail {open_t['_htexts'][-k:]}; boundary closed")})
                _close()
                open_t = _new_table(rec, len(tables), page, header, width, unit,
                                    f"new boundary: header mismatch on page {pno}")
            elif width == open_t["_width"]:
                open_t["stitch_log"].append({
                    "page": pno, "action": "append",
                    "reason": "no header on page; column count matches"})
                open_t["pages"].append(pno)
            else:
                open_t["stitch_log"].append({
                    "page": pno, "action": "reject",
                    "reason": f"no reprinted header and column count {width} != {open_t['_width']}"})
                _close()
                open_t = _new_table(rec, len(tables), page, header, width, unit,
                                    f"new boundary: column count changed on page {pno}")
        if unit and open_t["unit"] and unit != open_t["unit"]:
            open_t["stitch_log"].append({
                "page": pno, "action": "note",
                "reason": f"page unit {unit!r} differs from table unit {open_t['unit']!r}; table unit kept"})
        elif unit and not open_t["unit"]:
            open_t["unit"] = unit
        _process_body(open_t, body, page)
    _close()
    status = "ok" if not quarantine else ("partial" if tables else "failed")
    return {
        "tables": tables,
        "quarantine": quarantine,
        "_table_rebuild": {"version": VERSION, "status": status,
                           "table_count": len(tables),
                           "quarantined_pages": [q["page"] for q in quarantine]},
    }


def explain_stitching(rebuilt):
    """导出跨页拼接/拒绝拼接依据的可读说明（每行一条决定）。"""
    lines = []
    for t in rebuilt.get("tables", []):
        for e in t["stitch_log"]:
            lines.append(f"{t['table_id']} p{e['page']}: {e['action']} — {e['reason']}")
    for q in rebuilt.get("quarantine", []):
        lines.append(f"p{q['page']}: quarantined — {q['error']}")
    return "\n".join(lines)


def process(record):
    rec = dict(record)
    if not isinstance(rec.get("_ann_pages"), list) and not isinstance(rec.get("pages"), list):
        rec.setdefault("_stage_table_rebuild", "naive")  # 旧记录走旧路径，接口不变
        return rec
    rec.update(rebuild_announcement(rec))
    rec["_stage_table_rebuild"] = VERSION
    return rec

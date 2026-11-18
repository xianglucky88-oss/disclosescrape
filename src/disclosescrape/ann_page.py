"""单一职责：公告页序整理与来源标注（ann_page v2）。

含 pages 的公告记录：按页码确定性排序（页码缺失/非法的页排最后，同级保持原
先先后顺序），并为每页盖下来源（公告 id + 规范化 src）与页序 order，供
table_rebuild 写入每个重建单元格的 prov，实现数值反查原文。
不含 pages 的旧记录：行为与旧版一致（仅打 _stage 标记），旧接口不受影响。
"""

VERSION = "v2"


def order_pages(pages, rec):
    """把原始 pages 整理成带 provenance 的有序页序列（纯函数、确定性）。"""
    stamped = []
    for i, p in enumerate(pages or []):
        if isinstance(p, dict):
            page_no, table = p.get("page"), p.get("table")
        else:  # 非 dict 的页整体保留，交给 table_rebuild 隔离
            page_no, table = None, p
        if not isinstance(page_no, int) or isinstance(page_no, bool):
            page_no = None
        stamped.append({
            "ann": rec.get("id"),
            "src": rec.get("src"),
            "page": page_no,
            "orig_order": i,
            "table": table,
        })
    stamped.sort(key=lambda d: (d["page"] is None, d["page"] or 0, d["orig_order"]))
    for order, d in enumerate(stamped):
        d["order"] = order
    return stamped


def process(record):
    rec = dict(record)
    if not isinstance(rec.get("pages"), list):
        rec.setdefault("_stage_ann_page", "naive")
        return rec
    rec["_ann_pages"] = order_pages(rec.get("pages"), rec)
    rec["_stage_ann_page"] = VERSION
    return rec

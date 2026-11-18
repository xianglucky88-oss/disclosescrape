"""单一职责：对记录做单一职责变换：table_rebuild。

承接 ann_page 阶段对列表发现/详情取得/版本确认的注解，决定本阶段如何重建；
没有 ann_page 标记的旧离线输入保持 naive 路径，旧夹具输出不受影响。"""
from . import ann_page

def process(record):
    rec = dict(record)
    stage = rec.get("_stage_ann_page")
    if stage == "detail_fetch":
        rec["_stage_table_rebuild"] = "from_detail:" + str(rec.get("detail_status"))
        rec["table_rebuild_ready"] = rec.get("detail_status") == ann_page.OK
    elif stage == "list_discovery":
        rec["_stage_table_rebuild"] = "from_list"
        rec["table_rebuild_ready"] = False  # 列表条目本身不含表格，等待详情
    else:
        rec.setdefault("_stage_table_rebuild", "naive")
    return rec

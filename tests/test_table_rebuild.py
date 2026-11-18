"""table_rebuild v2：合并表头/括号负数/缺列/坏表混批检查；分批方式与故障恢复后
的表格边界、单位与落库内容一致性；坏表隔离与单独重试；provenance 反查原文。"""
import copy
import json
from pathlib import Path

import pytest

from src.disclosescrape import ann_page, loader, normalizer, pipeline, store, table_rebuild

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "input.json"


def raw(rid):
    for r in json.loads(FIX.read_text(encoding="utf-8")):
        if r.get("id") == rid:
            return copy.deepcopy(r)
    raise KeyError(rid)


def chain(rec):
    for fn in (normalizer.process, ann_page.process, table_rebuild.process):
        rec = fn(rec)
    return rec


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    return tmp_path / "test.db"


def stored():
    con = store.connect()
    try:
        return dict(store.all_records(con))
    finally:
        con.close()


def sorted_records():
    con = store.connect()
    try:
        return sorted(store.all_records(con))
    finally:
        con.close()


# ---------- 表头与合并单元格 ----------

def test_merged_and_multiline_header():
    rec = chain(raw("ann-101"))
    t = rec["tables"][0]
    assert [c["name"] for c in t["columns"]] == ["项目", "金额/本期金额", "金额/上期金额"]
    assert t["header"] == [["项目", "金额", "金额"], ["项目", "本期金额", "上期金额"]]
    assert t["unit"] == "万元"          # “单位：万元”独占行被识别为表级单位
    assert len(t["rows"]) == 4          # 单位行不进数据


# ---------- 括号/负号负数与单位换算可追溯 ----------

def test_paren_negative_and_unit_traceable():
    rec = chain(raw("ann-101"))
    rows = {r["label"]: r for r in rec["tables"][0]["rows"]}
    neg = rows["营业成本"]["cells"][1]
    assert neg["raw"] == "−6,000.00" and neg["num"] == -6000.0
    assert neg["v"] == neg["num"] * neg["scale"] == -60000000.0
    paren = rows["营业成本"]["cells"][2]
    assert paren["raw"] == "(5,400.00)" and paren["v"] == -54000000.0
    for cell in (neg, paren):
        assert cell["unit"] == "万元" and cell["scale"] == 10000
    mgmt = rows["管理费用"]["cells"][1]
    assert mgmt["raw"] == "(1,234.56)" and mgmt["num"] == -1234.56
    assert mgmt["v"] == pytest.approx(-12345600.0)


# ---------- 布局空白 ≠ 真实空值 ----------

def test_layout_blank_is_not_true_null():
    rec = chain(raw("ann-101"))
    rows = {r["label"]: r for r in rec["tables"][0]["rows"]}
    blank, null = rows["研发费用"]["cells"][1], rows["研发费用"]["cells"][2]
    assert blank["kind"] == "blank" and blank["raw"] == "" and blank["v"] is None
    assert null["kind"] == "null" and null["raw"] is None and null["v"] is None
    assert blank["kind"] != null["kind"]


# ---------- 跨页拼接：续页表头重印被剔除，依据可解释 ----------

def test_cross_page_stitch_drops_reprinted_header():
    rec = chain(raw("ann-101"))
    assert len(rec["tables"]) == 1
    t = rec["tables"][0]
    assert t["pages"] == [1, 2]
    assert [r["label"] for r in t["rows"]] == ["营业收入", "营业成本", "管理费用", "研发费用"]
    assert (2, "append") in [(e["page"], e["action"]) for e in t["stitch_log"]]
    assert "reprinted" in table_rebuild.explain_stitching(rec)
    assert t["footnotes"][0]["text"].startswith("注：")
    assert t["footnotes"][0]["prov"]["page"] == 2


def test_page_order_is_deterministic_regardless_of_input_order():
    a = chain(raw("ann-101"))
    shuffled = raw("ann-101")
    shuffled["pages"] = list(reversed(shuffled["pages"]))
    b = chain(shuffled)
    assert [p["page"] for p in a["_ann_pages"]] == [1, 2]   # 输入页序乱序被规整
    assert a["tables"] == b["tables"]                       # 边界与内容不随输入顺序变


# ---------- 拒绝拼接：列数/表头不同 → 确定性边界 ----------

def test_reject_stitch_gives_deterministic_boundary():
    rec = chain(raw("ann-102"))
    assert len(rec["tables"]) == 2
    t0, t1 = rec["tables"]
    assert (t0["table_id"], t0["pages"]) == ("ann-102#t0", [1])
    assert (t1["table_id"], t1["pages"]) == ("ann-102#t1", [2])
    assert any(e["action"] == "reject" and e["page"] == 2 for e in t0["stitch_log"])
    assert "reject" in table_rebuild.explain_stitching(rec)
    assert chain(raw("ann-102"))["tables"] == rec["tables"]  # 重放边界一致


# ---------- 列缺失：短行右侧补真实空值并标记 ----------

def test_missing_column_padded_with_true_null():
    rec = chain(raw("ann-102"))
    row = rec["tables"][0]["rows"][1]
    assert row["label"] == "应收账款" and row["missing_cols"] == 1
    pad = row["cells"][2]
    assert pad["kind"] == "null" and pad["missing_col"] is True and pad["v"] is None
    assert row["cells"][1]["v"] == 300.0


def test_percent_and_header_embedded_unit():
    rec = chain(raw("ann-102"))
    pct = rec["tables"][1]["rows"][0]["cells"][3]
    assert pct["unit"] == "%" and pct["v"] == 51.5
    rec3 = chain(raw("ann-103"))
    col = rec3["tables"][0]["columns"][1]
    assert col["name"] == "金额" and col["unit"] == "元"     # “金额（元）”拆出列单位
    assert rec3["tables"][0]["rows"][0]["cells"][1]["v"] == 42.0


# ---------- 坏表隔离，不污染其他公告 ----------

def test_corrupt_table_quarantined_others_unaffected(db):
    assert pipeline.run() == {"fed": 7, "rows": 6}
    data = stored()
    bad = data["ann-103"]
    assert bad["_table_rebuild"]["status"] == "partial"
    assert [q["page"] for q in bad["quarantine"]] == [1]
    assert len(bad["tables"]) == 1                       # 第 2 页好表正常落库
    assert data["ann-101"]["tables"][0]["pages"] == [1, 2]
    assert len(data["ann-102"]["tables"]) == 2
    assert data["r-001"]["_stage_table_rebuild"] == "naive"   # 旧记录走旧路径


def test_retry_fixed_table_does_not_touch_others(db, monkeypatch):
    pipeline.run()
    before = stored()
    fixed = raw("ann-103")
    fixed["pages"][0]["table"] = [["项目", "金额（元）"], ["总资产", "88.00"]]
    monkeypatch.setattr(loader, "load", lambda: [fixed])     # 单独重放修复后的公告
    con = store.connect()
    try:
        assert pipeline.run(con=con)["fed"] == 1
    finally:
        con.close()
    after = stored()
    assert len(after) == len(before) == 6                    # 行数不变，按键幂等覆盖
    good = after["ann-103"]
    assert good["quarantine"] == [] and good["_table_rebuild"]["status"] == "ok"
    assert [r["label"] for r in good["tables"][0]["rows"]] == ["总资产", "净利润"]
    for key in before:                                       # 其他公告内容不变
        if key != "ann-103":
            assert after[key] == before[key]


# ---------- 分批方式与故障恢复 ----------

@pytest.mark.parametrize("batch_size", [1, 2, 3, 100])
def test_batch_size_does_not_change_stored_content(tmp_path, monkeypatch, batch_size):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "ref.db")
    pipeline.run()
    ref = sorted_records()
    monkeypatch.setattr(store, "DB_PATH", tmp_path / f"b{batch_size}.db")
    pipeline.run(batch_size=batch_size)
    assert sorted_records() == ref   # 表格边界、单位与落库内容与分批方式无关


def test_fault_recovery_after_batch_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "clean.db")
    pipeline.run()
    clean = sorted_records()

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "fault.db")
    con = store.connect()
    calls = {"n": 0}
    orig = table_rebuild.process

    def flaky(rec):
        calls["n"] += 1
        if calls["n"] == 5:            # 第 5 条（ann-101）处理时注入故障
            raise RuntimeError("boom")
        return orig(rec)

    monkeypatch.setattr(table_rebuild, "process", flaky)
    with pytest.raises(RuntimeError):
        pipeline.run(con=con, batch_size=2)
    check = store.connect()            # 前两个批次已提交：3 个唯一键，故障不回滚
    assert store.count(check) == 3
    check.close()

    monkeypatch.setattr(table_rebuild, "process", orig)
    assert pipeline.run(con=con, batch_size=2) == {"fed": 7, "rows": 6}
    recovered = sorted(store.all_records(con))
    con.close()
    assert recovered == clean          # 恢复后落库内容与无故障运行一致


# ---------- provenance：normalizer 之后仍可反查原文 ----------

def test_cell_provenance_traces_back_to_source_text():
    rec = chain(raw("ann-101"))
    assert rec["value"] == 0           # 记录级字段已被 normalizer 规整
    rows = {r["label"]: r for r in rec["tables"][0]["rows"]}
    cell = rows["管理费用"]["cells"][1]
    assert cell["prov"] == {"ann": "ann-101", "src": "cninfo", "page": 2,
                            "order": 1, "row": 1, "col": 1}
    src_page = raw("ann-101")["pages"][0]          # 输入夹具中第 2 页写在最前
    assert src_page["page"] == 2
    prov = cell["prov"]
    assert src_page["table"][prov["row"]][prov["col"]] == "(1,234.56)" == cell["raw"]


def test_legacy_records_keep_legacy_path_and_shape(db):
    pipeline.run()
    data = stored()
    assert data["r-001"] == {
        "id": "r-001", "src": "alpha", "name": "Record One", "value": 10,
        "status": "active", "ts": "2026-11-01T00:00:00Z",
        "_stage_ann_page": "naive", "_stage_table_rebuild": "naive",
    }
    assert "tables" not in data["r-002"] and "quarantine" not in data["r-002"]

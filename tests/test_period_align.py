"""period_align 报告期归并的离线场景测试。

覆盖：跨年财季归位、同公告多表、公告/表内依据冲突裁决、重述版本关系、
缺失期间、期末时区、乱序到达、重复导入、中途恢复、日期格式漂移不覆盖证据。
"""
from src.disclosescrape import period_align, pipeline, store, keyer
from src.disclosescrape.models import Record

FIX = "fixtures/disclosures.json"


def _run(records, con):
    chain = pipeline._build_chain()
    for raw in records:
        rec = raw
        for fn in chain:
            rec = fn(rec)
        store.upsert(con, Record(key=keyer.key_of(rec), data=rec))
    store.commit(con)


def _load():
    import json
    from pathlib import Path
    return json.loads(Path(FIX).read_text(encoding="utf-8"))


def _by_id(rows, rid):
    return [d for k, d in rows if d.get("id") == rid][0]


# ---------------------------------------------------------------- 基础映射
def test_cross_year_fiscal_quarter_maps_to_fy_quarters():
    out = period_align.process(_load()[0])
    p = out["period"]
    # GLOBEX 财年 3-31 结束：2025-12-31 属于 FY2026 Q3（财年跨年）
    assert p["fiscal_year"] == 2026
    assert p["fiscal_period"] == "Q3"
    assert p["period_key"] == "fy2026-Q3"
    assert p["timeline_key"] == "2025-12-31"
    assert p["period_type"] == "quarter"
    assert p["period_missing"] is False


def test_quarter_index_inferred_from_dates_without_label():
    rec = {
        "src": "g", "name": "n",
        "announcement_id": "a", "table_id": "t",
        "publish_date": "2025-08-10",
        "period_start": "2025-04-01", "period_end": "2025-06-30",
        "fiscal_year_end": "03-31",
    }
    p = period_align.process(rec)["period"]
    assert (p["fiscal_year"], p["fiscal_period"]) == (2026, "Q1")
    assert p["basis"]["period_type"] == "inferred"


def test_annual_and_semiannual_periods_on_timeline():
    rows = _load()
    annual = period_align.process(rows[7])["period"]
    semi = period_align.process(rows[8])["period"]
    assert annual["period_key"] == "fy2025-FY"
    assert semi["period_key"] == "fy2025-H1"


def test_explicit_fiscal_coords_win():
    p = period_align.process(_load()[9])["period"]
    assert p["fiscal_year"] == 2025
    assert p["fiscal_period"] == "Q3"
    assert p["basis"]["coords"].startswith("explicit")
    # 嵌套 announcement/table 字典内的依据也要被采纳
    assert p["announcement_id"] == "Z-2025-012"
    assert p["table_id"] == "Z-T3"


# ---------------------------------------------------------------- 同公告多表
def test_same_announcement_multiple_tables_are_separate_rows():
    rows = _load()
    a = period_align.process(rows[0])
    b = period_align.process(rows[1])
    assert a["period"]["announcement_id"] == b["period"]["announcement_id"] == "A-2026-002"
    assert a["period"]["table_id"] != b["period"]["table_id"]
    # 共享同一期间坐标，但键因表身份不同而各自成行
    assert a["period_key"] == b["period_key"] == "fy2026-Q3"
    assert keyer.key_of(a) != keyer.key_of(b)


# ---------------------------------------------------------------- 冲突裁决
def test_conflicting_bases_keep_adjudication_reason():
    # r-106：表内期末 2025-06-30 与公告声称期间 2025-12-31 冲突
    p = period_align.process(_load()[5])["period"]
    assert p["period_end"] == "2025-06-30"          # 表内一手证据胜出
    reasons = [a["reason"] for a in p["adjudication"]]
    assert any("冲突" in r for r in reasons)
    winners = {a["winner"] for a in p["adjudication"]}
    assert "table" in winners
    # 两套依据都要留在证据里
    sources = {e["source"] for e in p["evidence"]}
    assert sources == {"table", "announcement"}


def test_publish_before_period_end_flagged_not_rewritten():
    rec = {
        "src": "g", "name": "n", "announcement_id": "a",
        "publish_date": "2025-01-01",
        "period_start": "2025-01-01", "period_end": "2025-03-31",
    }
    p = period_align.process(rec)["period"]
    assert p["period_end"] == "2025-03-31"
    assert p["publish_date"] == "2025-01-01"
    assert any("早于期末" in a["reason"] for a in p["adjudication"])


# ---------------------------------------------------------------- 重述关系
def test_restatement_is_new_version_not_new_period():
    rows = _load()
    original = period_align.process(rows[0])
    restated = period_align.process(rows[3])
    assert restated["period"]["is_restatement"] is True
    assert restated["period"]["restatement_of"] == "A-2026-002"
    # 同一期间坐标，不是新期间
    assert original["period_key"] == restated["period_key"] == "fy2026-Q3"
    # 键不同：原件与重述各自成行，重述不会覆盖原件
    ko, kr = keyer.key_of(original), keyer.key_of(restated)
    assert ko != kr
    assert "original-v1" in ko
    assert "restated" in kr


def test_restatement_flag_vs_link_conflict_adjudicated():
    rec = {
        "src": "g", "name": "n", "announcement_id": "a2", "table_id": "t",
        "is_restatement": False, "restatement_of": "A-1",
        "period_end": "2025-06-30", "fiscal_year_end": "03-31",
    }
    out = period_align.process(rec)
    assert out["is_restatement"] is True
    assert any(a["winner"] == "link" for a in out["period"]["adjudication"])


def test_restatement_relation_is_bidirectional_and_order_independent():
    rows = _load()
    pair = [r for r in rows if r["id"] in ("r-101", "r-104")]
    # 重述先于原件到达（旧期间晚于新期间到达的离线乱序）
    linked = period_align.link_restatements(list(reversed(pair)))
    by_id = {r["id"]: r for r in linked}
    original = by_id["r-101"]["period"]
    restated = by_id["r-104"]["period"]
    # 反向链接：重述指向原件
    assert restated["restatement_of"] == "A-2026-002"
    # 正向链接：原件记录其重述版本
    assert original["restated_by"] == [{
        "announcement_id": "A-2026-010",
        "table_id": "T-001",
        "version": 2,
        "period_key": "fy2026-Q3",
    }]
    # 乱序不影响链接结果
    linked2 = period_align.link_restatements(pair)
    by_id2 = {r["id"]: r for r in linked2}
    assert by_id2["r-101"]["period"]["restated_by"] == original["restated_by"]


def test_explicit_restatement_version_used_in_key():
    rec = {
        "src": "g", "name": "n", "announcement_id": "a3", "table_id": "t",
        "is_restatement": True, "version": 5,
        "period_start": "2025-10-01", "period_end": "2025-12-31",
        "fiscal_year_end": "03-31",
    }
    out = period_align.process(rec)
    assert out["period"]["version"] == 5
    assert out["period"]["version_basis"] == "explicit"
    assert "restated-v5" in keyer.key_of(out)


def test_compact_and_chinese_date_formats_normalize_to_same_identity():
    variants = [
        {"src": "g", "name": "n", "table_id": "t",
         "period_end": "2025-12-31", "fiscal_year_end": "12-31",
         "period_type": "Q4"},
        {"src": "g", "name": "n", "table_id": "t",
         "period_end": "20251231", "fiscal_year_end": "12月31日",
         "period_type": "四季报"},
        {"src": "g", "name": "n", "table_id": "t",
         "period_end": "2025.12.31", "fiscal_year_end": "12-31",
         "period_type": "quarter"},
    ]
    outs = [period_align.process(v) for v in variants]
    period_keys = {o["period_key"] for o in outs}
    assert period_keys == {"fy2025-Q4"}
    row_keys = {keyer.key_of(o) for o in outs}
    assert len(row_keys) == 1          # 三种写法 → 同一稳定键


def test_june_fiscal_year_end_cross_year_quarters():
    from datetime import date
    # 财年 6-30 结束：2025 日历年下半年属于 FY2026
    expected = {
        "2025-07-15": (2026, "Q1"), "2025-12-31": (2026, "Q2"),
        "2026-01-15": (2026, "Q3"), "2026-06-30": (2026, "Q4"),
    }
    for iso, want in expected.items():
        y, m, d = map(int, iso.split("-"))
        got = period_align.fiscal_coords(date(y, m, d), 6, 30, "quarter", "quarter")
        assert got == want


def test_unparseable_date_marks_missing_without_raising():
    out = period_align.process({"id": "x", "period_end": "not-a-date"})
    assert out["period"]["period_missing"] is True
    assert out["period"]["period_key"] is None
    assert keyer.key_of(out) == "x"      # 缺失期间回退到 id 键


def test_chinese_boolean_restatement_token():
    out = period_align.process({
        "id": "r", "period_end": "2025-12-31",
        "is_restatement": "是", "fiscal_year_end": "12-31",
    })
    assert out["is_restatement"] is True
    assert out["period_version"] == 2


# ---------------------------------------------------------------- 缺失期间
def test_missing_period_is_marked_not_invented():
    p = period_align.process(_load()[6])["period"]
    assert p["period_missing"] is True
    assert p["period_key"] is None
    assert p["fiscal_year"] is None
    assert any(a["winner"] == "none" for a in p["adjudication"])


def test_timeline_reports_gaps_and_missing():
    timeline = period_align.build_timeline(_load())
    missing_ids = {r["id"] for r in timeline["missing"]}
    assert "r-107" in missing_ids
    # GLOBEX（3-31 财年）只观察到 FY2026 Q1/Q3 → Q2、Q4 为缺口
    gap_keys = {g["period_key"] for g in timeline["gaps"] if g["src"] == "globex"}
    assert "fy2026-Q2" in gap_keys
    assert "fy2026-Q4" in gap_keys


# ---------------------------------------------------------------- 时区
def test_period_end_timezone_preserved_and_publish_utc_derived():
    p = period_align.process(_load()[0])["period"]
    assert p["period_end_tz"] == "Asia/Shanghai"
    # 2026-02-10T08:00:00+08:00 → 00:00Z
    assert p["publish_date_utc"] == "2026-02-10T00:00:00Z"
    # 期末归属仍按本地日期
    assert p["period_end"] == "2025-12-31"


# ---------------------------------------------------------------- 顺序无关
def test_timeline_order_independent_of_arrival():
    rows = _load()
    forward = period_align.build_timeline(rows)["points"]
    backward = period_align.build_timeline(list(reversed(rows)))["points"]
    assert [r["id"] for r in forward] == [r["id"] for r in backward]
    # 旧期间晚于新期间到达：排序后旧期间仍在时间轴前面
    ids = [r["id"] for r in forward]
    assert ids.index("r-103") < ids.index("r-101")


# ---------------------------------------------------------------- 格式漂移
def test_date_format_drift_keeps_same_key_and_evidence(tmp_path):
    con = store.connect(str(tmp_path / "drift.db"))
    try:
        rows = _load()
        _run([rows[0]], con)                       # ISO 写法先入库
        key_before = dict(store.all_records(con))
        _run([rows[4]], con)                       # 中文/斜杠/点号写法重导
        all_rows = dict(store.all_records(con))
        # 同一语义键，行数不增加
        assert set(all_rows) == set(key_before)
        only_key = next(iter(all_rows))
        stored = all_rows[only_key]
        # 规范化坐标一致
        assert stored["period"]["period_end"] == "2025-12-31"
        assert stored["period"]["period_key"] == "fy2026-Q3"
        # 两种原始写法都作为证据保留，首次证据未被覆盖
        raws = [e["raw"].get("period_end") for e in stored["raw_evidence"]]
        assert "2025-12-31" in raws and "2025/12/31" in raws
        assert stored["raw_evidence"][0]["raw"]["period_end"] == "2025-12-31"
    finally:
        con.close()


# ---------------------------------------------------------------- 重述不覆盖原件
def test_restatement_does_not_overwrite_original_evidence(tmp_path):
    con = store.connect(str(tmp_path / "restate.db"))
    try:
        rows = _load()
        _run([rows[0], rows[3]], con)             # 原件 + 重述
        all_rows = store.all_records(con)
        originals = [d for _, d in all_rows if not d["period"]["is_restatement"]]
        restatements = [d for _, d in all_rows if d["period"]["is_restatement"]]
        assert len(originals) == 1 and len(restatements) == 1
        assert originals[0]["id"] == "r-101"
        assert originals[0]["period"]["version"] == 1
        assert restatements[0]["id"] == "r-104"
        # 重述记录保留指向原件的关系
        assert restatements[0]["period"]["restatement_of"] == "A-2026-002"
    finally:
        con.close()


# ---------------------------------------------------------------- 重复导入
def test_duplicate_import_is_idempotent(tmp_path):
    con = store.connect(str(tmp_path / "dup.db"))
    try:
        rows = _load()
        _run(rows, con)
        first = store.count(con)
        _run(rows, con)                            # 全量重复导入
        assert store.count(con) == first
        keys1 = sorted(k for k, _ in store.all_records(con))
        _run(list(reversed(rows)), con)            # 乱序再来一遍
        assert store.count(con) == first
        assert sorted(k for k, _ in store.all_records(con)) == keys1
    finally:
        con.close()


# ---------------------------------------------------------------- 中途恢复
def test_mid_run_resume_completes_without_duplicates(tmp_path):
    con = store.connect(str(tmp_path / "resume.db"))
    try:
        rows = _load()
        half = len(rows) // 2
        _run(rows[:half], con)                     # 第一次跑到一半中断
        after_first = store.count(con)
        _run(rows, con)                            # 恢复后全量重放
        final = dict(store.all_records(con))
        assert store.count(con) >= after_first
        # 每个 id 恰好一行，无重复键
        ids = [d.get("id") for d in final.values()]
        assert sorted(ids) == sorted(set(ids))
    finally:
        con.close()


# ---------------------------------------------------------------- 季度汇总
def test_quarterly_summary_separates_original_and_restated():
    summary = period_align.summarize_by_quarter(_load())
    bucket = next(b for b in summary if b["period_key"] == "fy2026-Q3")
    # 原件 r-101=100、r-102=200（同公告不同表）、r-105=100（格式漂移孪生记录，
    # 落库时会与 r-101 合并为同一键）；重述 r-104=110 必须分列。
    assert bucket["original_value"] == 400
    assert bucket["restated_value"] == 110
    assert len(bucket["restatements"]) == 1
    assert bucket["restatements"][0]["restatement_of"] == "A-2026-002"
    # 发布日期可区分
    assert "2026-02-10" in bucket["publish_dates"]


# ---------------------------------------------------------------- 流水线接入
def test_pipeline_ingests_disclosure_fixture(tmp_path):
    con = store.connect(str(tmp_path / "pipe.db"))
    try:
        result = pipeline.run(con=con, source=FIX)
        assert result["fed"] == 10
        # r-101 与 r-105 是同一语义记录（格式漂移），落库后去重
        assert result["rows"] == 9
        stored = dict(store.all_records(con))
        sample = next(iter(stored.values()))
        assert "period" in sample and sample["period_key"]
    finally:
        con.close()


def test_baseline_fixture_still_uses_id_fallback(tmp_path):
    con = store.connect(str(tmp_path / "base.db"))
    try:
        result = pipeline.run(con=con, source="fixtures/input.json")
        assert result["fed"] == 4 and result["rows"] == 3
        keys = {k for k, _ in store.all_records(con)}
        assert {"r-001", "r-002", "r-003"} <= keys
    finally:
        con.close()


def test_pipeline_persists_bidirectional_restatement_links(tmp_path):
    db = str(tmp_path / "links.db")
    con = store.connect(db)
    try:
        pipeline.run(con=con, source=FIX)
        stored = {d["id"]: d for _, d in store.all_records(con)}
        original = stored["r-101"]["period"]
        restated = stored["r-104"]["period"]
        assert restated["restatement_of"] == "A-2026-002"
        assert any(x["announcement_id"] == "A-2026-010"
                   for x in original.get("restated_by", []))
    finally:
        con.close()
    # 重跑：链接幂等，不重复堆叠
    con = store.connect(db)
    try:
        pipeline.run(con=con, source=FIX)
        stored = {d["id"]: d for _, d in store.all_records(con)}
        links = stored["r-101"]["period"]["restated_by"]
        assert len(links) == 1
    finally:
        con.close()

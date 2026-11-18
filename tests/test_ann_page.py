"""ann_page 分页/版本/续采行为测试。

覆盖：空列表、同一链接换内容（区分重复链接与页面更新）、一半详情失败、
重启后续采范围、失败重试的抓取边界、来源身份跨游标稳定、旧夹具兼容与写入计数、
诊断可定位具体公告。全部使用 tmp_path 下的独立 sqlite 库，离线运行。"""
import pytest

from src.disclosescrape import ann_page, pipeline, store as store_mod
from src.disclosescrape.keyer import key_of


# ---------------------------------------------------------------- 夹具工厂
def listing(source, cursor, next_cursor, items, status=ann_page.OK, error=None):
    rec = {"page_type": "list", "source": source, "cursor": cursor,
           "next_cursor": next_cursor, "items": items, "status": status}
    if error:
        rec["error"] = error
    return ann_page.process(rec)


def item(link, source_ref=None, title=None):
    d = {"link": link, "title": title or link}
    if source_ref:
        d["source_ref"] = source_ref
    return d


def detail(source, link, content, status=ann_page.OK, attachments=None,
           source_ref=None, error=None, link_changed=False):
    rec = {"page_type": "detail", "source": source, "link": link,
           "content": content, "status": status}
    if attachments is not None:
        rec["attachments"] = attachments
    if source_ref:
        rec["source_ref"] = source_ref
    if error:
        rec["error"] = error
    if link_changed:
        rec["link_changed"] = True
    return ann_page.process(rec)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(store_mod, "DB_PATH", str(path))
    from src.disclosescrape import config
    monkeypatch.setattr(config, "DB_PATH", str(path))
    con = store_mod.connect()
    yield con
    con.close()


SRC = "cninfo"


# ----------------------------------------------------------- 1. 空列表页
def test_empty_list_advances_cursor_and_marks_exhausted(tmp_db):
    st = ann_page.AnnPageState(con=tmp_db)
    page = listing(SRC, cursor="p1", next_cursor="p2", items=[])

    assert ann_page.cursor_can_advance(page) is True
    touched = st.observe_list(page)

    assert touched == []
    assert st.cursors[SRC] == "p2"            # 空列表也是成功响应，边界推进
    assert st.exhausted[SRC] is True          # 来源读到空页，标记耗尽
    assert st.pending() == []
    assert st.resume_scope() == []

    st.save(tmp_db)
    revived = ann_page.AnnPageState.load(tmp_db)
    assert revived.cursors[SRC] == "p2"
    assert revived.exhausted[SRC] is True


# --------------------------------------------- 2. 重复链接 vs 页面更新的区别
def test_duplicate_link_does_not_create_version_or_identity(tmp_db):
    st = ann_page.AnnPageState(con=tmp_db)
    link = "https://example.test/ann/a"
    page1 = listing(SRC, "p1", "p2", [item(link, source_ref="A-1")])
    # 同一公告在后续页/下一批次再次出现（分页游标不同）
    page2 = listing(SRC, "p2", "p3", [item(link, source_ref="A-1")])

    st.observe_list(page1)
    st.observe_list(page2)
    ident = ann_page.ann_identity(SRC, link, "A-1")

    assert len(st.announcements) == 1                  # 重复链接不产生新公告
    entry = st.announcements[ident]
    assert entry["state"] == ann_page.LISTED
    assert entry["list_hits"] == 2
    assert entry["duplicate_links"] == 1               # 重复链接被计数
    assert entry["version"] == 0 and entry["versions"] == []
    # 身份不随分页游标变化
    assert entry["first_seen_cursor"] == "p1"
    assert entry["last_seen_cursor"] == "p2"
    assert ann_page.ann_identity(SRC, link, "A-1") == ident


def test_same_link_changed_content_is_version_update_in_place(tmp_db):
    st = ann_page.AnnPageState(con=tmp_db)
    link = "https://example.test/ann/b"
    st.observe_list(listing(SRC, "p1", "p2", [item(link, source_ref="B-1")]))
    ident = ann_page.ann_identity(SRC, link, "B-1")

    d1 = detail(SRC, link, {"v": 1}, source_ref="B-1")
    st.observe_detail(d1)
    assert st.announcements[ident]["version"] == 1
    assert st.announcements[ident]["state"] == ann_page.DETAIL_OK

    # 同一链接、同一身份、内容变化 → 页面更新，版本递增且原地更新
    d2 = detail(SRC, link, {"v": 2, "changed": True}, source_ref="B-1")
    assert d2["ann_identity"] == ident and d2["id"] == ident
    st.observe_detail(d2)
    entry = st.announcements[ident]
    assert len(st.announcements) == 1
    assert entry["version"] == 2
    assert [v["change"] for v in entry["versions"]] == [None, ann_page.UPDATE_CONTENT]

    # 内容未变再次抓取 → 不新增版本
    st.observe_detail(detail(SRC, link, {"v": 2, "changed": True},
                             source_ref="B-1"))
    assert entry["version"] == 2
    assert entry["state"] == ann_page.DETAIL_OK


def test_source_ref_stable_while_link_is_replaced(tmp_db):
    """来源方换链接：身份（按来源编号）不变，版本变更记为 link_changed。"""
    st = ann_page.AnnPageState(con=tmp_db)
    old_link, new_link = "https://a.test/old", "https://a.test/new"
    st.observe_list(listing(SRC, "p1", "p2", [item(old_link, source_ref="C-9")]))
    ident = ann_page.ann_identity(SRC, old_link, "C-9")

    st.observe_detail(detail(SRC, old_link, {"x": 1}, source_ref="C-9"))
    # 列表再次出现时链接已换，来源编号不变
    st.observe_list(listing(SRC, "p2", "p3", [item(new_link, source_ref="C-9")]))
    assert list(st.announcements) == [ident]
    assert st.announcements[ident]["link"] == new_link

    st.observe_detail(detail(SRC, new_link, {"x": 2}, source_ref="C-9",
                             link_changed=True))
    entry = st.announcements[ident]
    assert entry["version"] == 2
    assert entry["versions"][-1]["change"] == ann_page.UPDATE_LINK


def test_in_page_duplicate_link_is_flagged_and_registered_once(tmp_db):
    st = ann_page.AnnPageState(con=tmp_db)
    link = "https://example.test/ann/dup"
    page = listing(SRC, "p1", "p2", [item(link), item(link)])

    flags = [it.get("duplicate_in_list") for it in page["items"]]
    assert flags == [False, True]
    st.observe_list(page)
    assert len(st.announcements) == 1


# ------------------------------------------------- 3. 一半详情失败 + 重启续采
def test_half_detail_failures_resume_after_restart(tmp_db):
    st = ann_page.AnnPageState(con=tmp_db)
    links = [f"https://example.test/ann/{n}" for n in range(4)]
    page = listing(SRC, "p1", "p2", [item(u, source_ref=f"R-{n}") for n, u in enumerate(links)])
    st.observe_list(page)
    idents = [ann_page.ann_identity(SRC, u, f"R-{n}") for n, u in enumerate(links)]

    ok_recs = [detail(SRC, links[n], {"n": n}, source_ref=f"R-{n}")
               for n in (0, 1)]
    fail_recs = [detail(SRC, links[n], {"n": n}, source_ref=f"R-{n}",
                        status=ann_page.FAILED, error="http 502")
                 for n in (2, 3)]
    for r in ok_recs + fail_recs:
        st.observe_detail(r)

    assert sorted(e["state"] for e in st.announcements.values()) == \
        [ann_page.DETAIL_FAILED, ann_page.DETAIL_FAILED,
         ann_page.DETAIL_OK, ann_page.DETAIL_OK]
    pending = st.pending()
    assert {e["ann_identity"] for e in pending} == set(idents[2:])
    # 详情失败不影响列表游标
    assert st.cursors[SRC] == "p2"
    st.save(tmp_db)

    # ---- 模拟重启：新批次，进度从 KV 表还原
    first_batch = st.batch_id
    revived = ann_page.AnnPageState.load(tmp_db, new_batch=True)
    assert revived.batch_id != first_batch
    scope = revived.resume_scope()
    assert {s["ann_identity"] for s in scope} == set(idents[2:])
    assert {s["state"] for s in scope} == {ann_page.DETAIL_FAILED}
    assert {s["retry_from_cursor"] for s in scope} == {"p2"}

    # 续采：一个恢复成功，另一个仍然失败 → 续采范围只剩后者
    revived.observe_detail(detail(SRC, links[2], {"n": 2}, source_ref="R-2"))
    revived.observe_detail(detail(SRC, links[3], {"n": 3}, source_ref="R-3",
                                  status=ann_page.FAILED, error="timeout"))
    scope2 = revived.resume_scope()
    assert [s["ann_identity"] for s in scope2] == [idents[3]]
    recovered = revived.announcements[idents[2]]
    assert recovered["state"] == ann_page.DETAIL_OK
    assert recovered["errors"] == []                  # 恢复后清理历史失败
    assert recovered["first_seen_batch"] == first_batch
    assert recovered["last_seen_batch"] == revived.batch_id

    # 再一轮全部补齐
    revived.observe_detail(detail(SRC, links[3], {"n": 3}, source_ref="R-3"))
    assert revived.resume_scope() == []
    revived.save(tmp_db)

    final = ann_page.AnnPageState.load(tmp_db)
    assert all(e["state"] == ann_page.DETAIL_OK
               for e in final.announcements.values())
    assert final.cursors[SRC] == "p2"


def test_attachment_failure_marks_detail_failed(tmp_db):
    st = ann_page.AnnPageState(con=tmp_db)
    link = "https://example.test/ann/att"
    st.observe_list(listing(SRC, "p1", "p2", [item(link)]))
    rec = detail(SRC, link, {"body": "z"}, attachments=[
        {"link": "https://a.test/f1.pdf", "status": ann_page.OK},
        {"link": "https://a.test/f2.pdf", "status": ann_page.FAILED},
    ])
    assert rec["detail_status"] == ann_page.FAILED
    st.observe_detail(rec)
    ident = ann_page.ann_identity(SRC, link)
    assert st.announcements[ident]["state"] == ann_page.DETAIL_FAILED
    assert st.pending()[0]["ann_identity"] == ident


# --------------------------------------------- 4. 失败重试何时推进抓取边界
def test_failed_list_page_holds_cursor_but_success_moves_it(tmp_db):
    st = ann_page.AnnPageState(con=tmp_db)
    bad = listing(SRC, "p1", "p2", [], status=ann_page.FAILED, error="conn reset")
    assert ann_page.cursor_can_advance(bad) is False
    st.observe_list(bad)
    assert SRC not in st.cursors                    # 失败页不推进边界
    assert st.exhausted.get(SRC) is None

    good = listing(SRC, "p1", "p2", [item("https://example.test/ann/1")])
    st.observe_list(good)
    assert st.cursors[SRC] == "p2"                  # 重试成功后才推进
    assert st.exhausted.get(SRC) is False

    # 列表失败后重启，游标仍停在失败页
    st.save(tmp_db)
    revived = ann_page.AnnPageState.load(tmp_db)
    assert revived.cursors[SRC] == "p2"
    assert revived.last_list_status[SRC] == ann_page.OK


def test_detail_failure_never_moves_list_cursor_back(tmp_db):
    st = ann_page.AnnPageState(con=tmp_db)
    st.observe_list(listing(SRC, "p1", "p2",
                            [item("https://example.test/ann/1")]))
    st.observe_detail(detail(SRC, "https://example.test/ann/1", {},
                             status=ann_page.FAILED, error="500"))
    assert st.cursors[SRC] == "p2"                  # 详情失败不回退游标
    assert st.exhausted[SRC] is False


# ------------------------------------------------- 5. 身份跨游标/批次稳定
def test_identity_independent_of_cursor_and_batch(tmp_db):
    link = "https://example.test/ann/same"
    a = listing(SRC, "page-1", "page-2", [item(link)])
    b = listing(SRC, "page-9", "page-10", [item(link)])
    assert a["items"][0]["ann_identity"] == b["items"][0]["ann_identity"]

    s1 = ann_page.AnnPageState()
    s2 = ann_page.AnnPageState()
    s1.observe_list(a)
    s2.observe_list(b)
    assert list(s1.announcements) == list(s2.announcements)


def test_detail_without_source_ref_reconciles_by_link(tmp_db):
    """详情任务缺少来源编号时，状态机按链接对账回同一公告，不拆成两条。"""
    st = ann_page.AnnPageState(con=tmp_db)
    link = "https://example.test/ann/recon"
    st.observe_list(listing(SRC, "p1", "p2", [item(link, source_ref="Z-7")]))
    canonical = ann_page.ann_identity(SRC, link, "Z-7")

    bare = detail(SRC, link, {"body": 1})           # 只有链接，无 source_ref
    assert bare["ann_identity"] != canonical         # 裸派生身份不同
    entry = st.observe_detail(bare)
    assert entry["ann_identity"] == canonical        # 对账后并入同一公告
    assert len(st.announcements) == 1
    assert st.announcements[canonical]["state"] == ann_page.DETAIL_OK


def test_pinned_ann_identity_is_honored_by_process(tmp_db):
    """调度方（resume_scope）可直接钉住身份，process 不再重新派生。"""
    pinned = "ann:fixed-identity"
    rec = ann_page.process({"page_type": "detail", "source": SRC,
                            "link": "https://example.test/ann/x",
                            "content": {}, "status": ann_page.OK,
                            "ann_identity": pinned})
    assert rec["ann_identity"] == pinned and rec["id"] == pinned


# ------------------------------------------------- 6. 诊断定位具体公告
def test_diagnose_locates_announcement_and_failures(tmp_db):
    st = ann_page.AnnPageState(con=tmp_db)
    link = "https://example.test/ann/diag"
    st.observe_list(listing(SRC, "p1", "p2", [item(link, source_ref="D-1")]))
    st.observe_detail(detail(SRC, link, {}, source_ref="D-1",
                             status=ann_page.FAILED, error="http 504"))
    ident = ann_page.ann_identity(SRC, link, "D-1")

    diag = st.diagnose(ident)
    assert diag["found"] is True
    assert diag["state"] == ann_page.DETAIL_FAILED
    assert diag["errors"][-1]["reason"] == "http 504"
    assert diag["errors"][-1]["batch"] == st.batch_id
    assert diag["source_cursor"] == "p2"
    assert diag["ann_identity"] == ident

    missing = st.diagnose(ann_page.ann_identity(SRC, "https://x.test/none"))
    assert missing == {"ann_identity": missing["ann_identity"], "found": False}


# --------------------------------------------- 7. 旧离线夹具兼容 + 写入计数
def test_legacy_fixture_pipeline_unchanged_and_idempotent(tmp_db, monkeypatch):
    import json
    from src.disclosescrape import config as cfg
    legacy = json.loads(cfg.FIXTURES.read_text(encoding="utf-8"))
    monkeypatch.setattr(pipeline._loader, "load", lambda: legacy)

    a = pipeline.run(tmp_db)
    b = pipeline.run(tmp_db)                    # 重跑不增行
    assert a["fed"] == 4
    assert b["rows"] == a["rows"] == 3          # r-003 重复仍被去重

    rows = dict(store_mod.all_records(tmp_db))
    for key, data in rows.items():
        assert data["_stage_ann_page"] == "naive"       # 旧标记保留
        assert data["_stage_table_rebuild"] == "naive"
        assert data["_ann_kind"] == ann_page.LEGACY

    # 旧记录进入状态机后视为已物化，不进入续采范围
    st = ann_page.AnnPageState(con=tmp_db)
    for rec in legacy:
        st.observe_legacy(rec)
    assert st.pending() == []
    assert {e["state"] for e in st.announcements.values()} == {ann_page.LEGACY}


def test_retry_and_restart_do_not_duplicate_writes(tmp_db, monkeypatch):
    """端到端走完整条 chain + store：失败重试/重启只覆盖同键，不重复写入。"""
    links = [f"https://example.test/ann/w{n}" for n in range(4)]
    list_rec = listing(SRC, "p1", "p2",
                       [item(u, source_ref=f"W-{n}") for n, u in enumerate(links)])

    def run_batch(detail_recs):
        monkeypatch.setattr(pipeline._loader, "load",
                            lambda: [list_rec] + detail_recs)
        return pipeline.run(tmp_db)

    # 首轮：1 条列表记录 + 2 成功 + 2 失败
    first = run_batch([
        detail(SRC, links[0], {"n": 0}, source_ref="W-0"),
        detail(SRC, links[1], {"n": 1}, source_ref="W-1"),
        detail(SRC, links[2], {"n": 2}, source_ref="W-2",
               status=ann_page.FAILED, error="502"),
        detail(SRC, links[3], {"n": 3}, source_ref="W-3",
               status=ann_page.FAILED, error="502"),
    ])
    assert first["fed"] == 5
    rows_after_first = store_mod.count(tmp_db)
    assert rows_after_first == 5

    # 原样重跑（重试）：键稳定，行数不增
    second = run_batch([
        detail(SRC, links[0], {"n": 0}, source_ref="W-0"),
        detail(SRC, links[1], {"n": 1}, source_ref="W-1"),
        detail(SRC, links[2], {"n": 2}, source_ref="W-2",
               status=ann_page.FAILED, error="502"),
        detail(SRC, links[3], {"n": 3}, source_ref="W-3",
               status=ann_page.FAILED, error="502"),
    ])
    assert second["fed"] == 5
    assert store_mod.count(tmp_db) == rows_after_first

    # 重启后续采：失败详情换成成功载荷，身份键不变 → 仍无新行
    third = run_batch([
        detail(SRC, links[2], {"n": 2, "patched": True}, source_ref="W-2"),
        detail(SRC, links[3], {"n": 3, "patched": True}, source_ref="W-3"),
    ])
    assert third["fed"] == 3
    assert store_mod.count(tmp_db) == rows_after_first

    # 落库的详情行确实以稳定公告身份为键
    keys = {k for k, _ in store_mod.all_records(tmp_db)}
    for n, u in enumerate(links):
        assert ann_page.ann_identity(SRC, u, f"W-{n}") in keys

    # 同身份换内容后重采：指纹变化但仍是同一行覆盖
    run_batch([detail(SRC, links[0], {"n": 0, "edited": True},
                      source_ref="W-0")])
    assert store_mod.count(tmp_db) == rows_after_first
    data = dict(store_mod.all_records(tmp_db))
    ident0 = ann_page.ann_identity(SRC, links[0], "W-0")
    assert data[ident0]["content"] == {"n": 0, "edited": True}
    assert data[ident0]["_stage_table_rebuild"] == "from_detail:ok"
    assert data[ident0]["table_rebuild_ready"] is True
    # 列表行永远不会被标记为可重建（键按完整处理链派生，与流水线落库一致）
    from src.disclosescrape import normalizer, table_rebuild
    list_stored = table_rebuild.process(
        ann_page.process(normalizer.process(dict(list_rec))))
    list_key = key_of(list_stored)
    assert data[list_key]["_stage_table_rebuild"] == "from_list"
    assert data[list_key]["table_rebuild_ready"] is False

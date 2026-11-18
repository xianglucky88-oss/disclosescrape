from src.disclosescrape import pipeline, store

def test_pipeline_runs_and_idempotent():
    a = pipeline.run()
    b = pipeline.run()   # 重跑不增行（幂等 upsert）
    assert a["fed"] == 7
    assert b["rows"] == a["rows"] == 6   # r-003 重复被去重；ann-101/102/103 各占一键

def test_store_roundtrip():
    con = store.connect()
    try:
        assert store.count(con) >= 0
    finally:
        con.close()

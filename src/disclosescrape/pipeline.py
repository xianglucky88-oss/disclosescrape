"""薄编排：loader → normalizer→ann_page→table_rebuild → keyer → store。不含业务判断。

batch_size：可选的分批提交粒度（每 N 条 commit 一次）。只影响故障发生时的落库
粒度（已提交批次保留，重跑幂等恢复），不影响最终落库内容——各阶段均为纯函数。
"""
import importlib
from . import store as _store
from . import loader as _loader
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ['normalizer', 'ann_page', 'table_rebuild']

def run(con=None, batch_size=None):
    own = con is None
    con = con or _store.connect()
    try:
        chain = []
        for n in CHAIN_NAMES:
            mod = importlib.import_module("." + n, __package__)
            chain.append(mod.process)
        fed = pending = 0
        for raw in _loader.load():
            rec = raw
            for fn in chain:
                rec = fn(rec)
            _store.upsert(con, Record(key=key_of(rec), data=rec))
            fed += 1
            pending += 1
            if batch_size and pending >= batch_size:
                _store.commit(con)
                pending = 0
        _store.commit(con)
        return {"fed": fed, "rows": _store.count(con)}
    finally:
        if own:
            con.close()

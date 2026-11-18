"""薄编排：loader → normalizer → period_align → ann_page → table_rebuild → keyer → store。

不含业务判断：每个业务阶段各自一个模块、一个 process 函数，
本文件只按顺序调用并把键值与数据交给存储。
period_align.link_restatements 是该模块提供的批量装配（补全重述正向链接），
属于期间归并阶段自身的能力，编排层只负责调用，不写业务规则。
"""
import importlib
from . import store as _store
from . import loader as _loader
from . import period_align as _period_align
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ['normalizer', 'period_align', 'ann_page', 'table_rebuild']


def _build_chain():
    return [importlib.import_module("." + n, __package__).process
            for n in CHAIN_NAMES]


def run(con=None, source=None):
    """运行离线流程。

    con/sqlite 连接：不传则使用默认库并在结束时关闭。
    source：可选的夹具路径或已加载记录列表，供离线多场景测试注入，
    默认走 loader 读取 config.FIXTURES。
    """
    own = con is None
    con = con or _store.connect()
    try:
        chain = _build_chain()
        raws = _loader.load(source) if source is not None else _loader.load()
        enriched = []
        for raw in raws:
            rec = raw
            for fn in chain:
                rec = fn(rec)
            enriched.append(rec)
        # 重述关系双向链接（乱序安全：与到达顺序无关）
        enriched = _period_align.link_restatements(enriched)
        fed = 0
        for rec in enriched:
            _store.upsert(con, Record(key=key_of(rec), data=rec))
            fed += 1
        _store.commit(con)
        return {"fed": fed, "rows": _store.count(con)}
    finally:
        if own:
            con.close()

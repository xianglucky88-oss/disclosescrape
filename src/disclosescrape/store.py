"""单一职责：本地 sqlite3 持久化，提供幂等 upsert / 计数 / 全量读取。

upsert 采用**合并语义**而非盲覆盖：
- 规范化字段以最新裁决结果刷新；
- 原始证据（period.evidence / raw_evidence / 历史时间戳）只增不改，
  日期格式变化、乱序重导、中途恢复都不会抹掉已有历史证据；
- 重述记录与原件键不同，天然各自成行，原件不会被重述覆盖。
"""
import json, sqlite3
from .config import DB_PATH


def connect(db_path=DB_PATH):
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    return con


def _merge_data(existing, incoming):
    if not isinstance(existing, dict) or not isinstance(incoming, dict):
        return incoming
    merged = dict(existing)
    for key, value in incoming.items():
        if key == "period" and isinstance(value, dict) and isinstance(merged.get("period"), dict):
            merged["period"] = _merge_period(merged["period"], value)
        elif key == "raw_evidence":
            merged["raw_evidence"] = _union_evidence(merged.get("raw_evidence"), value)
        elif key == "evidence_history":
            merged["evidence_history"] = _union_evidence(merged.get("evidence_history"), value)
        elif key in _FIRST_WINS_FIELDS and merged.get(key) not in (None, ""):
            # 身份字段首见为准：格式漂移/重复导入不改写已有记录身份
            continue
        else:
            merged[key] = value
    return merged


# 首见即冻结的身份字段，避免重复导入或写法变化改写既有记录身份。
_FIRST_WINS_FIELDS = frozenset({"id", "announcement_id"})


def _merge_period(old, new):
    out = dict(old)
    for key, value in new.items():
        if key in ("evidence", "adjudication", "restated_by"):
            out[key] = _union_evidence(old.get(key), value)
        else:
            out[key] = value
    return out


def _union_evidence(old, new):
    """证据并集：去重保留首次出现顺序，历史证据永不被覆盖。"""
    if old is None:
        return new
    if new is None:
        return old
    seen = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in old}
    union = list(old)
    for item in new:
        token = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if token not in seen:
            seen.add(token)
            union.append(item)
    return union


def upsert(con, record):
    row = con.execute("SELECT data FROM records WHERE key=?", (record.key,)).fetchone()
    if row is None:
        con.execute("INSERT INTO records(key,data) VALUES(?,?)",
                    (record.key, json.dumps(record.data, ensure_ascii=False)))
        return "inserted"
    existing = json.loads(row[0])
    merged = _merge_data(existing, record.data)
    con.execute("UPDATE records SET data=? WHERE key=?",
                (json.dumps(merged, ensure_ascii=False), record.key))
    return "merged"


def commit(con): con.commit()


def count(con): return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]

"""单一职责：本地 sqlite3 持久化，提供幂等 upsert / 计数 / 全量读取。

另有与业务记录表隔离的通用 KV 表，供 ann_page 阶段持久化分页游标、
公告发现/详情状态与抓取批次；旧夹具与旧库表结构保持兼容。"""
import json, sqlite3
from .config import DB_PATH

STATE_TABLE = "ann_page_state"

def connect():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS " + STATE_TABLE +
        "(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    return con

def upsert(con, record):
    con.execute("INSERT INTO records(key,data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (record.key, json.dumps(record.data, ensure_ascii=False)))

def commit(con): con.commit()

def count(con): return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]

# --- 与业务记录表隔离的 KV 存取（ann_page 分页/状态/批次持久化） ---
def kv_put(con, key, value, table=STATE_TABLE):
    con.execute(
        "INSERT INTO " + table + "(k,v) VALUES(?,?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, json.dumps(value, ensure_ascii=False)))

def kv_get(con, key, default=None, table=STATE_TABLE):
    row = con.execute("SELECT v FROM " + table + " WHERE k=?", (key,)).fetchone()
    return default if row is None else json.loads(row[0])

def kv_all(con, table=STATE_TABLE):
    return [(k, json.loads(v)) for k, v in
            con.execute("SELECT k,v FROM " + table + " ORDER BY k")]

def kv_clear(con, table=STATE_TABLE):
    con.execute("DELETE FROM " + table)

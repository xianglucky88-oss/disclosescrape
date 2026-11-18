"""单一职责：公告页面抽取阶段——列表发现、详情取得、版本确认的状态管理。

设计边界（与 README/任务约定一致）：
- 本模块沿用既有单记录入口 ``process(record)``，只对记录做注解式变换，
  不直接落库；跨记录/跨重启的状态判断由 ``AnnPageState`` 承担，
  持久化交给 ``store``，下游表格重建交给 ``table_rebuild``。
- 来源公告身份（``ann_identity``）只由来源站点 + 公告链接/来源编号派生，
  **不含分页游标、批次或内容摘要**，因此同一公告翻页位置变化、内容更新、
  换批次重采，身份保持不变；分页游标单独存放，绝不参与身份派生。
- “重复链接”与“页面更新”严格区分：
    * 重复链接 = 同一来源身份再次在列表上出现（游标位置/批次可能不同），
      状态机只去重、刷新游标观测，不产生新版本；
    * 页面更新 = 身份不变但详情内容指纹变化，追加 ``versions`` 版本史、
      版本号递增，并原地更新同一条记录（不新增行）。
- 抓取边界推进规则（``cursor_can_advance``）：
    * 列表页请求成功（含空列表）才允许把游标推进到下一页；
    * 列表页请求失败：游标原地不动，下一轮从同一页重试；
    * 详情/附件下载失败与列表游标完全解耦：不回退、不阻塞游标推进，
      公告停在 ``DETAIL_FAILED``，进入下一轮续采范围。
- 重试与恢复幂等：所有记录的稳定键来自身份（见 keyer），store 为 upsert，
  重试/重启重复观察同一公告不会产生重复写入。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from . import store as _store

# --- 公告在本阶段的生命周期状态 ---
LISTED = "listed"            # 仅在列表页发现，详情尚未取得
DETAIL_OK = "detail_ok"      # 详情页与全部附件下载成功，版本已确认
DETAIL_FAILED = "detail_failed"  # 详情页或至少一个附件下载失败
LEGACY = "legacy"            # 旧离线输入：详情已随夹具物化，无需采集

# 详情结果状态（记录载荷上的 detail_status）
OK = "ok"
FAILED = "failed"

# 页面更新类型（诊断用）
UPDATE_CONTENT = "content_updated"     # 同身份、同链接、内容变化
UPDATE_LINK = "link_changed"           # 同身份、链接被来源方更换
UNCHANGED = "unchanged"

_ID_PREFIX = "ann:"
_LIST_PREFIX = "annlist:"
_BATCH_PREFIX = "batch:"
_K_BATCH = "meta:batch"
_K_CURSOR = "cursor:"
_K_ANN = "ann:"


def _new_batch_id() -> str:
    return _BATCH_PREFIX + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + \
        "-" + uuid.uuid4().hex[:8]


def canonical_link(link) -> str:
    """规范化公告链接：去空白；空链接返回空串。"""
    return str(link or "").strip()


def ann_identity(source, link, source_ref=None) -> str:
    """派生来源公告稳定身份。

    仅依赖来源站点 + 规范化链接（或来源方编号），不含分页游标/批次/内容，
    保证同一公告跨页、跨批次、跨内容版本的身份恒定。
    """
    link = canonical_link(link)
    ref = str(source_ref or "").strip()
    basis = ref or link
    if not basis:
        raise ValueError("ann_identity 需要 link 或 source_ref 至少一个非空")
    raw = str(source or "").strip().lower() + "|" + basis
    return _ID_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def list_identity(source, cursor) -> str:
    """列表页观测记录的稳定身份（游标属于列表侧，不进入公告身份）。"""
    raw = str(source or "").strip().lower() + "|cursor=" + str(cursor)
    return _LIST_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def content_fingerprint(detail) -> str:
    """对详情正文与附件链接做规范化指纹；不含状态/时间戳/批次等易变字段。"""
    payload = detail.get("content")
    if isinstance(payload, dict):
        payload = {k: payload[k] for k in sorted(payload)}
    attachments = detail.get("attachments") or []
    att = sorted(
        canonical_link(a.get("link") if isinstance(a, dict) else a)
        for a in attachments
    )
    raw = repr(payload) + "|" + repr(att)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _attachments_ok(detail) -> bool:
    for a in detail.get("attachments") or []:
        if isinstance(a, dict) and a.get("status") == FAILED:
            return False
    return True


def detail_status(detail) -> str:
    """详情页与任一附件下载失败即视为失败；否则成功。"""
    if detail.get("status") == FAILED or not _attachments_ok(detail):
        return FAILED
    return OK


def cursor_can_advance(list_record) -> bool:
    """抓取边界是否允许推进：仅当列表页请求本身成功（空列表也算成功）。

    详情/附件失败不参与本判断——它们在公告维度重试，不拖回列表游标。
    """
    return list_record.get("status") != FAILED


def process(record):
    """单记录变换入口（保持单参数签名，兼容既有 pipeline 与旧离线输入）。

    依据 ``page_type`` 注解列表发现记录、详情记录；旧夹具记录走 legacy 路径，
    仅追加阶段标记，原有字段与键派生方式完全不变。
    """
    rec = dict(record)
    page_type = rec.get("page_type")

    if page_type == "list":
        source = rec.get("source")
        rec["_stage_ann_page"] = "list_discovery"
        rec["list_identity"] = list_identity(source, rec.get("cursor"))
        seen = set()
        for item in rec.get("items") or []:
            if not isinstance(item, dict):
                continue
            link = canonical_link(item.get("link"))
            ident = ann_identity(source, link, item.get("source_ref"))
            item["ann_identity"] = ident
            item["duplicate_in_list"] = ident in seen
            seen.add(ident)  # 同一列表页内重复链接：标记去重，不重复发现
        return rec

    if page_type == "detail":
        source = rec.get("source")
        link = canonical_link(rec.get("link"))
        # 调度方从列表条目/resume_scope 派发详情任务时可直接钉住身份，
        # 保证列表（按 source_ref）与详情（只有链接）两条路径身份一致。
        ident = rec.get("ann_identity") or ann_identity(
            source, link, rec.get("source_ref"))
        rec["_stage_ann_page"] = "detail_fetch"
        rec["ann_identity"] = ident
        rec["detail_status"] = detail_status(rec)
        rec["content_fingerprint"] = content_fingerprint(rec)
        rec.setdefault("id", ident)  # 让 keyer 以稳定身份落键，重试不产生新行
        return rec

    # 旧离线输入（已物化的公告记录）：不改变任何既有字段
    rec.setdefault("_stage_ann_page", "naive")
    rec.setdefault("_ann_kind", LEGACY)
    return rec


class AnnPageState:
    """ann_page 阶段的跨记录状态机：发现、详情、版本、游标、批次、诊断。

    状态保存在内存字典中，通过 ``save``/``load`` 与 sqlite KV 表交换，
    重启后可精确还原续采范围；本类不直接写业务 records 表。
    """

    def __init__(self, batch_id=None, con=None):
        self.batch_id = batch_id or _new_batch_id()
        self.con = con
        self.announcements: dict[str, dict] = {}
        self.cursors: dict[str, object] = {}      # source -> 下一次要抓的游标
        self.exhausted: dict[str, bool] = {}      # source -> 列表是否已读到空页
        self.last_list_status: dict[str, str] = {}

    # ---------------------------------------------------------------- 观察
    def observe_list(self, rec) -> list[dict]:
        """观察一张列表页记录，登记其中的公告；返回本页新发现/更新的公告。"""
        source = str(rec.get("source") or "")
        self.last_list_status[source] = rec.get("status", OK)
        touched: list[dict] = []
        if cursor_can_advance(rec):
            # 成功（含空列表）才推进边界；空列表标记来源耗尽
            self.cursors[source] = rec.get("next_cursor")
            self.exhausted[source] = not bool(rec.get("items"))
            for item in rec.get("items") or []:
                if isinstance(item, dict):
                    touched.append(self._register_from_list(source, item, rec))
        # 列表失败：游标、耗尽标记均不动，下一轮从同一页重试
        return touched

    def _register_from_list(self, source, item, list_rec) -> dict:
        ident = item.get("ann_identity") or ann_identity(
            source, item.get("link"), item.get("source_ref"))
        link = canonical_link(item.get("link"))
        existing = self.announcements.get(ident)
        if existing is None:
            entry = {
                "ann_identity": ident,
                "source": source,
                "source_ref": str(item.get("source_ref") or "").strip(),
                "link": link,
                "state": LISTED,
                "version": 0,
                "versions": [],
                "first_seen_batch": self.batch_id,
                "first_seen_cursor": list_rec.get("cursor"),
                "last_seen_batch": self.batch_id,
                "last_seen_cursor": list_rec.get("cursor"),
                "list_hits": 1,
                "duplicate_links": 0,
                "errors": [],
            }
            self.announcements[ident] = entry
            return entry
        # 重复链接：同身份再次出现，仅累计观测与刷新游标，不产生新版本
        existing["list_hits"] += 1
        existing["duplicate_links"] += 1
        existing["last_seen_batch"] = self.batch_id
        existing["last_seen_cursor"] = list_rec.get("cursor")
        if link and existing["link"] and link != existing["link"]:
            existing["link"] = link  # 来源方换链：身份不变，记录新链接
        return existing

    def _find_announcement(self, source, link, source_ref):
        """按来源编号优先、链接其次找回已发现公告（身份对账）。"""
        ref = str(source_ref or "").strip()
        for e in self.announcements.values():
            if e["source"] != source:
                continue
            if ref and e.get("source_ref") == ref:
                return e
        if link:
            for e in self.announcements.values():
                if e["source"] == source and e.get("link") == link:
                    return e
        return None

    def observe_detail(self, rec) -> dict:
        """观察一条详情记录，确认版本并更新公告状态（幂等，可安全重试）。"""
        ident = rec["ann_identity"]
        source = str(rec.get("source") or "")
        link = canonical_link(rec.get("link"))
        fp = rec["content_fingerprint"]
        status = rec["detail_status"]
        now = _utcnow()

        entry = self.announcements.get(ident)
        if entry is None:
            # 对账：详情路径未携带列表身份时，按链接/来源编号找回同一公告，
            # 避免同一公告因身份派生基不同被拆成两条。
            match = self._find_announcement(source, link, rec.get("source_ref"))
            if match is not None:
                ident = match["ann_identity"]
                rec["ann_identity"] = ident
                rec["id"] = ident
                entry = match
        if entry is None:
            # 详情先于列表到达（或重启后仅续采详情）：以详情补建身份
            entry = {
                "ann_identity": ident,
                "source": source,
                "source_ref": str(rec.get("source_ref") or "").strip(),
                "link": link,
                "state": LISTED,
                "version": 0,
                "versions": [],
                "first_seen_batch": self.batch_id,
                "first_seen_cursor": None,
                "last_seen_batch": self.batch_id,
                "last_seen_cursor": None,
                "list_hits": 0,
                "duplicate_links": 0,
                "errors": [],
            }
            self.announcements[ident] = entry

        entry["last_seen_batch"] = self.batch_id
        if link and entry["link"] and link != entry["link"]:
            entry["link"] = link

        if status == FAILED:
            # 下载失败：不推进版本，记录可定位的错误，等待下一轮续采
            entry["state"] = DETAIL_FAILED
            entry["errors"].append({
                "batch": self.batch_id, "at": now,
                "reason": rec.get("error") or "detail_or_attachment_download_failed",
            })
            return entry

        latest_fp = entry["versions"][-1]["fingerprint"] if entry["versions"] else None
        if latest_fp is None:
            change = None  # 首次成功取得详情
        elif fp == latest_fp:
            change = UNCHANGED
        else:
            change = UPDATE_LINK if rec.get("link_changed") else UPDATE_CONTENT

        if change != UNCHANGED:
            entry["version"] += 1
            entry["versions"].append({
                "version": entry["version"],
                "fingerprint": fp,
                "batch": self.batch_id,
                "at": now,
                "change": change,
            })
        entry["state"] = DETAIL_OK
        entry["errors"] = []  # 恢复成功后清掉历史失败标记
        return entry

    def observe_legacy(self, rec) -> dict:
        """登记旧离线输入记录：视为已物化成功，不进入待续采范围。"""
        ident = str(rec.get("id") or "")
        entry = {
            "ann_identity": ident,
            "source": str(rec.get("src") or ""),
            "source_ref": "",
            "link": "",
            "state": LEGACY,
            "version": 1,
            "versions": [{
                "version": 1, "fingerprint": None,
                "batch": self.batch_id, "at": _utcnow(), "change": None,
            }],
            "first_seen_batch": self.batch_id,
            "first_seen_cursor": None,
            "last_seen_batch": self.batch_id,
            "last_seen_cursor": None,
            "list_hits": 0,
            "duplicate_links": 0,
            "errors": [],
        }
        self.announcements[ident] = entry
        return entry

    # ------------------------------------------------------------ 续采/诊断
    def pending(self) -> list[dict]:
        """下一轮采集需要补详情的公告：已发现但详情未成功（含失败重试）。"""
        return [e for e in self.announcements.values()
                if e["state"] in (LISTED, DETAIL_FAILED)]

    def resume_scope(self) -> list[dict]:
        """重启后的续采范围：待补公告 + 每个来源的列表游标与耗尽标记。"""
        return [
            {
                "ann_identity": e["ann_identity"],
                "source": e["source"],
                "link": e["link"],
                "state": e["state"],
                "retry_from_cursor": self.cursors.get(e["source"]),
            }
            for e in self.pending()
        ]

    def diagnose(self, ident) -> dict:
        """定位具体公告：返回状态、版本史、重复链接计数、错误与游标观测。"""
        entry = self.announcements.get(ident)
        if entry is None:
            return {"ann_identity": ident, "found": False}
        out = dict(entry)
        out["found"] = True
        out["source_cursor"] = self.cursors.get(entry["source"])
        out["source_exhausted"] = self.exhausted.get(entry["source"], False)
        return out

    # ------------------------------------------------------------ 持久化
    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "announcements": self.announcements,
            "cursors": self.cursors,
            "exhausted": self.exhausted,
            "last_list_status": self.last_list_status,
        }

    @classmethod
    def from_dict(cls, data: dict, con=None):
        st = cls(batch_id=data.get("batch_id"), con=con)
        st.announcements = dict(data.get("announcements") or {})
        st.cursors = dict(data.get("cursors") or {})
        st.exhausted = dict(data.get("exhausted") or {})
        st.last_list_status = dict(data.get("last_list_status") or {})
        return st

    def save(self, con=None) -> None:
        """分片写入 KV 表；重试/重启重复保存只覆盖同键，不产生重复行。"""
        con = con or self.con
        if con is None:
            raise RuntimeError("save 需要 sqlite 连接")
        _store.kv_put(con, _K_BATCH, self.batch_id)
        _store.kv_put(con, _K_CURSOR, {
            "cursors": self.cursors,
            "exhausted": self.exhausted,
            "last_list_status": self.last_list_status,
        })
        _store.kv_put(con, _K_ANN, self.announcements)
        _store.commit(con)

    @classmethod
    def load(cls, con, new_batch=False):
        """从 KV 表还原状态；new_batch=True 时开启新抓取批次但保留全部进度。"""
        batch_id = _store.kv_get(con, _K_BATCH)
        announcements = _store.kv_get(con, _K_ANN, default={})
        cursor_doc = _store.kv_get(con, _K_CURSOR, default={})
        if batch_id is None and not announcements:
            return cls(con=con)
        st = cls.from_dict({
            "batch_id": batch_id,
            "announcements": announcements,
            "cursors": (cursor_doc or {}).get("cursors", {}),
            "exhausted": (cursor_doc or {}).get("exhausted", {}),
            "last_list_status": (cursor_doc or {}).get("last_list_status", {}),
        }, con=con)
        if new_batch:
            st.batch_id = _new_batch_id()
        return st


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

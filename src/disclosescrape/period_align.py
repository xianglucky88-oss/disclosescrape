"""单一职责：报告期归并（period_align）。

把公告元数据（发布日期、公告声称期间、重述标记）与表内期间信息
（表名/期间类型/起止日期/期末时区）共同裁决成一条**标准时间轴**上的
规范期间身份，并把期间身份与重述关系写入记录，供 keyer / store 使用。

设计不变量（对应题目要求）：
1. 期间身份只依赖规范化的时间轴坐标，不依赖原始日期字符串写法，
   因此日期格式变化不会产生新键、也不会覆盖原始证据。
2. 重述（restatement）是同一期间的新版本，永远不是新期间：
   重述记录与原件共享 period_key，但带不同的版本键，原件不被覆盖。
3. 公告依据与表内依据冲突时，按优先级裁决并把裁决原因完整保留
   （period.adjudication + period.evidence）。
4. 缺失期间不臆造：标记 period_missing=True，进入时间轴缺口序列。
5. 同公告多表：表身份进入键，多表各自成行；跨年财季按各自财年归位。

模块对外入口：
- process(record)：单条记录变换，流水线调用。
- build_timeline(records)：把已归并记录映射到统一时间轴（排序+缺口检测）。
- summarize_by_quarter(records)：按季度汇总披露数据（重述与原件分列）。
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# 字段别名：兼容平铺字段与嵌套 {"announcement": {...}} / {"table": {...}}
# ---------------------------------------------------------------------------
ANNOUNCEMENT_ALIASES = (
    "announcement_id", "ann_id", "announce_id",
    "公告编号", "公告ID",
)
PUBLISH_ALIASES = (
    "publish_date", "published_at", "announce_date", "announcement_date",
    "disclosure_date", "release_date", "publish_time", "publish_ts",
    "发布日期", "公告日期", "披露日期",
)
ANN_PERIOD_ALIASES = (
    "ann_period", "announcement_period", "ann_report_period",
    "announcement_report_period", "ann_period_end", "ann_period_label",
    "公告期间", "公告报告期",
)
TABLE_ID_ALIASES = ("table_id", "tid", "table_name", "表ID", "表编号", "表名")
PERIOD_TYPE_ALIASES = (
    "period_type", "report_type", "period_label", "report_period_type",
    "期间类型", "报告类型",
)
PERIOD_START_ALIASES = (
    "period_start", "start_date", "report_start", "begin_date",
    "开始日期", "期初",
)
PERIOD_END_ALIASES = (
    "period_end", "end_date", "report_end", "report_date",
    "截止日期", "期末日期", "期末",
)
RESTATEMENT_ALIASES = (
    "is_restatement", "restatement", "restated", "is_restated",
    "amended", "is_amendment", "revision", "is_revision",
    "是否重述", "重述", "更正",
)
RESTATEMENT_OF_ALIASES = (
    "restatement_of", "restates", "original_id", "amends", "supersedes",
    "重述对象", "原公告编号",
)
VERSION_ALIASES = ("version", "restatement_version", "版本", "重述版本")
TZ_ALIASES = (
    "period_end_tz", "end_tz", "timezone", "tz", "期末时区", "时区",
)
FY_END_ALIASES = (
    "fiscal_year_end", "fy_end", "fye", "财年结束月", "财年末",
)
FY_ALIASES = ("fiscal_year", "fy", "财年")
FP_ALIASES = ("fiscal_period", "fperiod", "财季")

# 期间类型词表
PERIOD_TYPE_WORDS = {
    "annual": "annual", "yearly": "annual", "year": "annual", "fy": "annual",
    "年度": "annual", "年报": "annual", "全年": "annual", "财年": "annual",
    "semi": "semiannual", "semiannual": "semiannual",
    "half_year": "semiannual", "halfyear": "semiannual", "h1": "semiannual",
    "半年度": "semiannual", "半年报": "semiannual", "中报": "semiannual",
    "quarter": "quarter", "quarterly": "quarter", "q1": "quarter",
    "q2": "quarter", "q3": "quarter", "q4": "quarter",
    "季度": "quarter", "季报": "quarter", "一季报": "quarter",
    "二季报": "quarter", "三季报": "quarter", "四季报": "quarter",
    "ytd": "ytd", "cumulative": "ytd", "累计": "ytd",
}
QUARTER_WORD_INDEX = {
    "q1": 1, "q2": 2, "q3": 3, "q4": 4,
    "一季报": 1, "一季度": 1, "第一季度": 1,
    "二季报": 2, "二季度": 2, "第二季度": 2,
    "三季报": 3, "三季度": 3, "第三季度": 3,
    "四季报": 4, "四季度": 4, "第四季度": 4,
}
# 不带序号的泛化季度词：不能据此确定是第几季，必须回到日期推断。
GENERIC_QUARTER_WORDS = ("quarter", "quarterly", "季度", "季报")
_TRUE_TOKENS = {"true", "1", "yes", "y", "是", "重述", "更正", "restated",
                "restatement", "amended", "amendment"}
_FALSE_TOKENS = {"false", "0", "no", "n", "否", "original", "原始"}

_SENTINEL = object()
_DATE_RE = re.compile(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})")
_OFFSET_RE = re.compile(r"([+-]\d{2}:?\d{2}|Z)$")


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _lookup(record, aliases, default=None):
    """按别名顺序取字段；同时在嵌套 announcement/table 字典内查找。"""
    for key in aliases:
        if key in record and record[key] not in (None, ""):
            return record[key]
    for nest_key in ("announcement", "table", "ann", "公告"):
        nest = record.get(nest_key)
        if isinstance(nest, dict):
            for key in aliases:
                if key in nest and nest[key] not in (None, ""):
                    return nest[key]
    return default


def _clean(value):
    if value is None:
        return ""
    return str(value).strip()


def _as_bool(value):
    if isinstance(value, bool):
        return value
    token = _clean(value).lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    return bool(token)


def parse_date(value, tz_hint=None):
    """宽容解析日期/时间戳。

    返回 (date|None, iso_utc|None, canonical_iso|None, note)。
    - 只有日期：canonical_iso 为 YYYY-MM-DD。
    - 带时间与时区：换算 UTC 瞬时，canonical_iso 保留原始本地日期的 ISO 表达，
      期末归属按本地日期（period-end tz），UTC 仅作发布排序依据。
    """
    raw = _clean(value)
    if not raw:
        return None, None, None, "empty"
    # 紧凑 8 位日期 YYYYMMDD（可带时间后缀）先归一为带分隔符形式
    compact = re.match(r"^(\d{4})(\d{2})(\d{2})(.*)$", raw.replace("T", " "))
    if compact:
        text = "%s-%s-%s%s" % compact.groups()
    else:
        text = raw.replace("T", " ")
    m = _DATE_RE.match(text)
    if not m:
        return None, None, None, "unparseable:%s" % raw
    year, month, day = (int(x) for x in m.groups())
    try:
        d = date(year, month, day)
    except ValueError:
        return None, None, None, "invalid-calendar:%s" % raw

    rest = text[m.end():].strip(" 日]")
    hh = mm = ss = 0
    has_clock = False
    clock = re.match(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", rest)
    if clock:
        has_clock = True
        hh, mm = int(clock.group(1)), int(clock.group(2))
        ss = int(clock.group(3) or 0)

    offset = _parse_offset(rest) or _parse_offset(tz_hint)
    if has_clock and offset is not None:
        dt_local = datetime(year, month, day, hh, mm, ss)
        dt_utc = dt_local - offset
        return d, dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), d.isoformat(), "ok"
    if has_clock:
        # 有时钟无时区：保留本地时间，UTC 不可得
        dt_local = datetime(year, month, day, hh, mm, ss)
        return d, None, dt_local.strftime("%Y-%m-%dT%H:%M:%S"), "naive-clock"
    return d, None, d.isoformat(), "ok"


def _parse_offset(text):
    if text is None:
        return None
    token = _clean(text).upper().replace(" ", "")
    if not token:
        return None
    if token.endswith("Z") or token in ("UTC", "GMT"):
        return timedelta(0)
    m = re.search(r"([+-])(\d{2}):?(\d{2})", token)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    if token in ("CST", "ASIA/SHANGHAI", "ASIA/CHONGQING", "ASIA/HARBIN"):
        return timedelta(hours=8)
    if token in ("JST",):
        return timedelta(hours=9)
    if token in ("PST", "AMERICA/LOS_ANGELES"):
        return timedelta(hours=-8)
    if token in ("EST", "AMERICA/NEW_YORK"):
        return timedelta(hours=-5)
    return None


def parse_fiscal_year_end(value):
    """返回 (month, day)；默认 12-31；只给月份则取该月最后一天。"""
    raw = _clean(value)
    if not raw:
        return 12, 31
    m = _DATE_RE.match(raw.replace("T", " "))
    if m:
        y, mo, da = (int(x) for x in m.groups())
        return mo, da
    md = re.match(r"^(\d{1,2})\s*月\s*(\d{1,2})\s*日?$", raw)
    if md:
        return int(md.group(1)), int(md.group(2))
    mm = re.match(r"^(\d{1,2})(?:-(\d{1,2}))?$", raw)
    if mm:
        month = int(mm.group(1))
        day = int(mm.group(2)) if mm.group(2) else calendar.monthrange(2000, month)[1]
        return month, day
    return 12, 31


def normalize_period_type(value):
    raw = _clean(value)
    if not raw:
        return None, raw
    token = raw.lower().replace("-", "_").replace(" ", "_")
    if token in PERIOD_TYPE_WORDS:
        return PERIOD_TYPE_WORDS[token], raw
    for word, canonical in PERIOD_TYPE_WORDS.items():
        if word in token:
            return canonical, raw
    return None, raw


def _quarter_index_from_label(raw):
    token = _clean(raw).lower().replace("-", "_").replace(" ", "")
    if not token or token in GENERIC_QUARTER_WORDS:
        return None
    if token in QUARTER_WORD_INDEX:
        return QUARTER_WORD_INDEX[token]
    # 先匹配带序号的长词，避免“季报”这类泛化词抢先命中。
    for word in sorted(QUARTER_WORD_INDEX, key=len, reverse=True):
        if word in token:
            return QUARTER_WORD_INDEX[word]
    if any(word in token for word in GENERIC_QUARTER_WORDS):
        return None
    return None


# ---------------------------------------------------------------------------
# 财年/财季坐标
# ---------------------------------------------------------------------------
def fiscal_coords(d, fye_month, fye_day, period_type=None, label_raw=None):
    """把日历日期映射到 (fiscal_year, fiscal_period)。

    财年按**结束日所在日历年**命名：结束日在 1..fye_month 的，归入上一日历年
    命名的财年；结束日晚于 fye_month 的，归入本日历命名的财年。
    例：3-31 财年下，2025-12-31 与 2026-01-15 都属 FY2026（跨年财季归位）。
    """
    if d is None:
        return None, None
    # 财年以其结束日所在日历年命名：结束日在 fye 及之前 → 属于以本日历收尾的财年；
    # 结束日晚于 fye → 属于下一财年（日历年 +1）。
    # 例：3-31 财年下 2025-06-30/2025-12-31 均属 FY2026（跨年财季归位），
    # 2026-03-31 也属 FY2026，2026-04-01 才进入 FY2027。
    fy = d.year if (d.month, d.day) <= (fye_month, fye_day) else d.year + 1
    # 该财年的日历起止
    fy_start = date(fy - 1, fye_month, fye_day) + timedelta(days=1)
    fy_end = date(fy, fye_month, fye_day)
    if period_type in ("quarter", "ytd") or (period_type is None and label_raw):
        # 仅当标签显式带序号（Q3/三季报）时直接采用；泛化标签（季报/quarter）
        # 不带序号，必须回到日期推断，不能默认落到 Q4。
        idx = _quarter_index_from_label(label_raw)
        if idx is None:
            idx = _infer_quarter_index(d, fy_start, fy_end, fye_month, fye_day)
        return fy, "Q%d" % idx
    if period_type == "semiannual":
        mid = fy_start + (fy_end - fy_start) / 2
        return fy, "H1" if d <= mid else "H2"
    if period_type == "annual":
        return fy, "FY"
    # 无类型：按跨度推断
    idx = _infer_quarter_index(d, fy_start, fy_end, fye_month, fye_day)
    return fy, "Q%d" % idx


def _infer_quarter_index(d, fy_start, fy_end, fye_month, fye_day):
    # 各季度结束月 = 财年起始月依次前进 3/6/9 个月，用绝对月序号（1-12）处理跨年；
    # 季度边界取该月最后一天的次日，与财年结束日的“日”无关。
    base_index = fy_start.year * 12 + fy_start.month
    boundaries = [fy_start]
    for k in (1, 2, 3):
        end_index = base_index + 3 * k - 1          # Qk 结束月的绝对序号
        y, m = (end_index - 1) // 12, end_index % 12 or 12
        last = calendar.monthrange(y, m)[1]
        boundaries.append(date(y, m, last) + timedelta(days=1))
    boundaries.append(fy_end + timedelta(days=1))
    for i, b in enumerate(boundaries[:-1], start=1):
        if b <= d < boundaries[i]:
            return i
    return 4


def _period_months(start, end):
    if start is None or end is None:
        return None
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def infer_period_type(start, end, label_raw=None):
    idx = _quarter_index_from_label(label_raw) if label_raw else None
    if idx is not None:
        return "quarter"
    canonical, _ = normalize_period_type(label_raw)
    if canonical in ("annual", "semiannual", "quarter", "ytd"):
        return canonical
    months = _period_months(start, end)
    if months is None:
        return None
    if months >= 11:
        return "annual"
    if months >= 5:
        return "semiannual"
    if months <= 4:
        return "quarter"
    return "ytd"


# ---------------------------------------------------------------------------
# 证据收集与裁决
# ---------------------------------------------------------------------------
def _adjudicate(conflicts, winner, reason):
    entry = {"winner": winner, "reason": reason, "conflicts": conflicts}
    return entry


# ---------------------------------------------------------------------------
# 主入口：单条记录归并
# ---------------------------------------------------------------------------
def process(record):
    if not isinstance(record, dict):
        record = {"value": record}
    rec = dict(record)
    adjudication = []
    evidence = []

    ann_id = _clean(_lookup(rec, ANNOUNCEMENT_ALIASES, ""))
    table_id = _clean(_lookup(rec, TABLE_ID_ALIASES, ""))
    fye_month, fye_day = parse_fiscal_year_end(_lookup(rec, FY_END_ALIASES, ""))
    tz_raw = _clean(_lookup(rec, TZ_ALIASES, ""))

    # ---- 发布日期：公告元数据为权威依据，表内字段仅作旁证 ----
    pub_raw = _lookup(rec, PUBLISH_ALIASES, "")
    pub_date, pub_utc, pub_iso, pub_note = parse_date(pub_raw, tz_raw)
    if pub_raw:
        evidence.append({"source": "announcement",
                         "fields": {"publish_date": pub_raw},
                         "note": pub_note})

    # ---- 表内期间依据 ----
    t_type_raw = _lookup(rec, PERIOD_TYPE_ALIASES, "")
    t_start_raw = _lookup(rec, PERIOD_START_ALIASES, "")
    t_end_raw = _lookup(rec, PERIOD_END_ALIASES, "")
    start_d, start_utc, start_iso, start_note = parse_date(t_start_raw, tz_raw)
    end_d, end_utc, end_iso, end_note = parse_date(t_end_raw, tz_raw)
    t_type, t_type_norm_raw = normalize_period_type(t_type_raw)
    if t_type_raw or t_start_raw or t_end_raw:
        snap_fields = {}
        for aliases in (PERIOD_TYPE_ALIASES, PERIOD_START_ALIASES, PERIOD_END_ALIASES):
            for k in aliases:
                if rec.get(k) not in (None, ""):
                    snap_fields[k] = rec[k]
        evidence.append({"source": "table",
                         "fields": snap_fields,
                         "note": ";".join(x for x in (start_note, end_note)
                                          if x and x != "ok")})

    # ---- 公告声称期间依据 ----
    ann_period_raw = _lookup(rec, ANN_PERIOD_ALIASES, "")
    ann_period_d, _, ann_period_iso, ann_period_note = parse_date(ann_period_raw, tz_raw)
    ann_type, _ = normalize_period_type(ann_period_raw)
    if ann_period_raw:
        evidence.append({"source": "announcement",
                         "fields": {"ann_period": ann_period_raw},
                         "note": ann_period_note})

    # ---- 显式财年/财季（最高优先的坐标依据）----
    fy_explicit = _lookup(rec, FY_ALIASES, _SENTINEL)
    fp_explicit = _clean(_lookup(rec, FP_ALIASES, ""))
    if fy_explicit is not _SENTINEL:
        try:
            fy_explicit = int(str(fy_explicit).strip().replace("FY", ""))
        except (TypeError, ValueError):
            fy_explicit = None

    # ---- 期间类型裁决：表内类型 > 公告类型 > 起止跨度推断 ----
    period_type = t_type
    type_basis = "table"
    if period_type is None and ann_type is not None:
        period_type, type_basis = ann_type, "announcement"
    if period_type is None:
        guessed = infer_period_type(start_d, end_d, t_type_raw or ann_period_raw)
        if guessed:
            period_type, type_basis = guessed, "inferred"
            adjudication.append(_adjudicate(
                [{"source": "table", "value": t_type_norm_raw},
                 {"source": "announcement", "value": ann_period_raw}],
                "inferred", "两处均无可用期间类型，按起止日期跨度推断为 %s" % guessed))
    elif ann_type is not None and ann_type != period_type:
        adjudication.append(_adjudicate(
            [{"source": "table", "value": t_type_norm_raw, "canonical": t_type},
             {"source": "announcement", "value": ann_period_raw,
              "canonical": ann_type}],
            "table", "期间类型冲突：表内期间是一手表格证据，优先于公告元数据"))

    # ---- 期末日裁决：表内期末 > 公告声称期间 ----
    period_end_d = end_d
    end_basis = "table" if end_d is not None else None
    if end_d is None and ann_period_d is not None:
        period_end_d, end_basis = ann_period_d, "announcement"
        adjudication.append(_adjudicate(
            [{"source": "table", "value": t_end_raw or None},
             {"source": "announcement", "value": ann_period_raw}],
            "announcement", "表内期末缺失，采用公告声称期间作为期末日"))
    if end_d is not None and ann_period_d is not None and end_d != ann_period_d:
        adjudication.append(_adjudicate(
            [{"source": "table", "value": t_end_raw, "iso": end_iso},
             {"source": "announcement", "value": ann_period_raw,
              "iso": ann_period_iso}],
            "table", "期末日冲突：表内期间为一手证据，公告声称期间仅作旁证"))

    # ---- 发布日期晚于/早于期末的合理性检查（保留原因，不静默改写）----
    if pub_date and period_end_d and pub_date < period_end_d:
        adjudication.append(_adjudicate(
            [{"source": "announcement", "publish_date": pub_iso},
             {"source": "table", "period_end": end_iso}],
            "table", "发布日期早于期末日，按各自依据原样保留并标记异常"))

    # ---- 财年/财季坐标 ----
    label_for_quarter = t_type_raw or ann_period_raw
    has_explicit_fy = fy_explicit is not _SENTINEL and fy_explicit is not None
    if has_explicit_fy and fp_explicit:
        fy, fp = fy_explicit, fp_explicit.upper()
        coord_basis = "explicit"
    elif has_explicit_fy and period_end_d is not None:
        _, fp = fiscal_coords(period_end_d, fye_month, fye_day,
                              period_type, label_for_quarter)
        fy, fp = fy_explicit, fp
        coord_basis = "explicit-fy+date"
    elif period_end_d is not None:
        fy, fp = fiscal_coords(period_end_d, fye_month, fye_day,
                               period_type, label_for_quarter)
        coord_basis = "derived"
    else:
        fy, fp, coord_basis = None, None, "unavailable"

    period_missing = period_end_d is None
    if period_missing:
        adjudication.append(_adjudicate(
            [{"source": "table", "period_end": t_end_raw or None},
             {"source": "announcement", "ann_period": ann_period_raw or None}],
            "none", "所有依据均未给出可用期末日，标记缺失期间，不臆造时间轴坐标"))

    period_key = None
    timeline_key = None
    if not period_missing:
        period_key = "fy%d-%s" % (fy, fp or period_type or "P")
        timeline_key = period_end_d.isoformat()

    # ---- 重述关系：两套依据冲突也要裁决并留痕 ----
    rest_flag_ann = _lookup(rec, RESTATEMENT_ALIASES, _SENTINEL)
    rest_bool = _as_bool(rest_flag_ann) if rest_flag_ann is not _SENTINEL else False
    rest_of = _clean(_lookup(rec, RESTATEMENT_OF_ALIASES, ""))
    if rest_flag_ann is not _SENTINEL and rest_of and not rest_bool:
        rest_bool = True
        adjudication.append(_adjudicate(
            [{"source": "flag", "value": rest_flag_ann},
             {"source": "link", "restatement_of": rest_of}],
            "link", "重述标记为否但存在重述指向链接，以链接为准判定为重述"))
    if rest_bool and not rest_of:
        adjudication.append(_adjudicate(
            [{"source": "flag", "value": rest_flag_ann},
             {"source": "link", "restatement_of": None}],
            "flag", "仅有重述标记、无原件指向，按重述版本入库并等待原件关联"))

    version_raw = _lookup(rec, VERSION_ALIASES, _SENTINEL)
    if version_raw is not _SENTINEL:
        try:
            version = int(str(version_raw).strip())
        except (TypeError, ValueError):
            version = 1
        version_basis = "explicit"
    else:
        version = 2 if rest_bool else 1
        version_basis = "default"
    is_restatement = bool(rest_bool or rest_of)

    # ---- 组装规范期间块（原始字段全部保留，不删不改）----
    period_block = {
        "period_key": period_key,
        "timeline_key": timeline_key,
        "period_type": period_type,
        "period_start": start_iso,
        "period_end": period_end_d.isoformat() if period_end_d else None,
        "period_end_utc": end_utc,
        "period_end_tz": tz_raw or None,
        "fiscal_year": fy,
        "fiscal_period": fp,
        "fiscal_year_end": "%02d-%02d" % (fye_month, fye_day),
        "publish_date": pub_iso,
        "publish_date_utc": pub_utc,
        "announcement_id": ann_id or None,
        "table_id": table_id or None,
        "is_restatement": is_restatement,
        "restatement_of": rest_of or None,
        "version": version,
        "version_basis": version_basis,
        "period_missing": period_missing,
        "basis": {
            "period_type": type_basis,
            "period_end": end_basis,
            "coords": coord_basis,
        },
        "evidence": evidence,
        "adjudication": adjudication,
    }
    rec["period"] = period_block
    # 顶层镜像，方便 keyer 与下游聚合直接消费
    rec["period_key"] = period_key
    rec["timeline_key"] = timeline_key
    rec["is_restatement"] = is_restatement
    rec["restatement_of"] = rest_of or None
    rec["period_version"] = version
    # 原始证据快照：单元素列表，store 合并时按并集去重，
    # 首次见到的写法永久保留，格式变化只新增快照、不覆盖。
    rec["raw_evidence"] = [{"raw": dict(record)}]
    return rec


# ---------------------------------------------------------------------------
# 重述关系双向链接（批量、无状态 process 之外的可选装配）
# ---------------------------------------------------------------------------
def link_restatements(records):
    """在已归并记录集合上补全重述关系的正向链接。

    重述记录自身已带 restatement_of（反向链接，指向原件公告）；
    本函数在原件的 period 块上追加 restated_by 列表（正向链接），
    使“期间身份 + 重述关系”在存储中双向可查，且不改动任何原始证据。
    """
    enriched = [r if isinstance(r, dict) and "period" in r else process(r)
                for r in records]
    index = {}
    for r in enriched:
        p = r["period"]
        ann = p.get("announcement_id")
        if ann:
            index.setdefault(ann, []).append(r)

    for r in enriched:
        p = r["period"]
        if not p.get("is_restatement"):
            continue
        target_ann = p.get("restatement_of")
        for original in index.get(target_ann, []):
            op = original["period"]
            if op.get("period_key") != p.get("period_key"):
                continue
            linked = op.setdefault("restated_by", [])
            pointer = {
                "announcement_id": p.get("announcement_id"),
                "table_id": p.get("table_id"),
                "version": p.get("version"),
                "period_key": p.get("period_key"),
            }
            token = repr(pointer)
            if token not in {repr(x) for x in linked}:
                linked.append(pointer)
    return enriched


# ---------------------------------------------------------------------------
# 统一时间轴：排序 + 缺口检测
# ---------------------------------------------------------------------------
def build_timeline(records):
    """把已归并记录映射到统一时间轴。

    返回 {"points": [...], "missing": [...]}：
    - points 按 (期末日, 发布UTC, 公告号, 表号, 版本) 稳定排序，与到达顺序无关；
    - missing 给出缺失期间记录；
    - gaps 给出同一主体相邻财季之间的断档坐标。
    """
    enriched = [r if isinstance(r, dict) and "period" in r else process(r)
                for r in records]

    def sort_key(r):
        p = r["period"]
        return (
            p.get("period_end") or "9999-12-31",
            p.get("publish_date_utc") or p.get("publish_date") or "",
            p.get("announcement_id") or "",
            p.get("table_id") or "",
            p.get("version") or 1,
        )

    points = sorted(enriched, key=sort_key)
    missing = [r for r in points if r["period"].get("period_missing")]

    # 缺口检测：同 src 下同财年相邻财季断档（src 已由 normalizer 归一为小写）
    seen = {}
    gaps = []
    for r in points:
        p = r["period"]
        if p.get("period_missing") or p.get("fiscal_period") in (None, "FY", "H1", "H2"):
            continue
        if not str(p.get("fiscal_period", "")).startswith("Q"):
            continue
        subject = (str(r.get("src", "")).strip().lower(),
                   p["fiscal_year"], p["fiscal_year_end"])
        q = int(p["fiscal_period"][1:])
        seen.setdefault(subject, set()).add(q)
    for (src, fy, fye), quarters in seen.items():
        for q in range(1, 5):
            if q not in quarters:
                gaps.append({"src": src, "fiscal_year": fy,
                             "fiscal_year_end": fye,
                             "missing_fiscal_period": "Q%d" % q,
                             "period_key": "fy%d-Q%d" % (fy, q)})
    return {"points": points, "missing": missing, "gaps": gaps}


# ---------------------------------------------------------------------------
# 按季度汇总（重述与原件分列，重述不覆盖原始证据）
# ---------------------------------------------------------------------------
def summarize_by_quarter(records):
    """季度汇总前先分清覆盖期间 / 发布日 / 是否重述。"""
    timeline = build_timeline(records)
    summary = {}
    for r in timeline["points"]:
        p = r["period"]
        key = p.get("period_key")
        if not key or not str(p.get("fiscal_period", "")).startswith("Q"):
            continue
        bucket = summary.setdefault(key, {
            "period_key": key,
            "fiscal_year": p.get("fiscal_year"),
            "fiscal_period": p.get("fiscal_period"),
            "period_end": p.get("period_end"),
            "original_value": 0,
            "restated_value": 0,
            "restatements": [],
            "publish_dates": [],
        })
        value = r.get("value", 0)
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 0
        if p.get("is_restatement"):
            bucket["restated_value"] += value
            bucket["restatements"].append({
                "announcement_id": p.get("announcement_id"),
                "table_id": p.get("table_id"),
                "version": p.get("version"),
                "value": value,
                "restatement_of": p.get("restatement_of"),
            })
        else:
            bucket["original_value"] += value
        pub = p.get("publish_date")
        if pub and pub not in bucket["publish_dates"]:
            bucket["publish_dates"].append(pub)
    return [summary[k] for k in sorted(summary)]

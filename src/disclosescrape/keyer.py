"""单一职责：派生稳定主键。

基线行为：有原始 id 时直接用 id；否则对记录内容做哈希。
period_align 接入后，已归并出规范期间身份的记录改用语义键：
期间坐标（period_key）+ 主体 + 表身份 + 重述版本。
语义键只依赖规范化坐标，因此：
- 原始日期写法变化（2026/1/5 ↔ 2026-01-05）不会换键，已有记录不会被覆盖；
- 重述与原件 period_key 相同但版本段不同 → 重述是新版本而非新期间，
  且永远不会覆盖原件；
- 到达顺序不参与键，离线乱序/重复导入/中途恢复结果一致。
"""
import hashlib, json


def _hash_tail(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def key_of(record):
    period = record.get("period") if isinstance(record, dict) else None
    if isinstance(period, dict) and period.get("period_key"):
        parts = [
            "per",
            str(record.get("src", "") or ""),
            str(record.get("name", "") or ""),
            str(period["period_key"]),
            str(period.get("table_id") or ""),
        ]
        if period.get("is_restatement"):
            # 重述版本段：显式版本优先；无显式版本时混入公告/表身份，
            # 保证同一期间的多次重述各自成行且重复导入键不变。
            version = period.get("version")
            if period.get("version_basis") == "explicit" and version is not None:
                parts.append("restated-v%s" % version)
            else:
                identity = "%s|%s" % (period.get("announcement_id") or "",
                                      period.get("table_id") or "")
                parts.append("restated-v%s-%s" % (version or 2,
                                                   _hash_tail(identity)))
        else:
            parts.append("original-v%s" % (period.get("version") or 1))
        return ":".join(parts)

    rid = record.get("id")
    if rid:
        return str(rid)
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]

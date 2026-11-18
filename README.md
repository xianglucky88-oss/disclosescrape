# disclosescrape — 抓披露平台公告（离线已抽取表格夹具），科目对齐与报告期归并、识别重复版本。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线和幂等性检查
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`subject_map`, `period_align`, `ann_page`, `table_rebuild`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 模块说明
业务模块按独立阶段组织，通用模块负责加载、规范化、主键、存储和编排。
流水线按配置顺序调用处理阶段，输入输出均遵循项目定义的记录结构。

### period_align：报告期归并（已实现）
把公告元数据（发布日期、公告声称期间、重述标记）与表内期间信息（期间类型、
起止日期、期末时区、财年结束月）共同裁决成一条**标准时间轴**上的规范期间身份。

- 入口：`period_align.process(record)` 单条归并；`build_timeline(records)`
  排序并检测缺失/缺口；`summarize_by_quarter(records)` 按季度汇总（重述与原件分列）；
  `link_restatements(records)` 补全重述双向链接。
- 规范期间块写入 `record["period"]`，并镜像 `period_key / timeline_key /
  is_restatement / restatement_of / period_version` 到顶层；原始字段与原始写法
  快照（`raw_evidence`）原样保留。
- 期间身份只依赖规范化坐标（`fy{财年}-{Qn|H1|FY}`），与原始日期字符串写法、
  到达顺序无关：日期格式变化不产生新键，也不覆盖已有证据。
- 重述是同一期间的新版本而非新期间：与原件共享 `period_key`，键中带版本段，
  原件永不被覆盖；`restatement_of`（反向）与 `restated_by`（正向）双向留痕。
- 公告依据与表内依据冲突时按「表内一手证据 > 公告声称 > 跨度推断」裁决，
  冲突双方与裁决原因保留在 `period.evidence` / `period.adjudication`。
- 缺失期间标记 `period_missing=True` 且不臆造坐标；时间轴 `gaps` 给出同主体
  财年内的缺季报财季；期末时区保留并派生发布时刻 UTC。
- 跨年财季按各自财年归位（如 3-31 财年下 2025-12-31 属 FY2026 Q3）；同公告
  多表以 `table_id` 入键、各自成行。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；upsert 采用合并语义：
规范化字段刷新、原始证据只增不改（并集去重），身份字段首见冻结。
测试夹具位于项目测试目录，离线多场景夹具见 `fixtures/disclosures.json`，
运行方式以 `Makefile` 为准。

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

## 披露表格重建（ann_page → table_rebuild）
- `ann_page` 把公告 pages 按页码确定性排序，并为每页盖下来源（公告 id + src）与页序；
  不含 pages 的旧记录走旧路径，行为不变。
- `table_rebuild` 将页内网格重建为边界确定的逻辑表：合并单元格按 rowspan/colspan 展开；
  多行表头逐列拼接为列名；“单位：X”独占行与表头“（单位）”识别为表级/列单位；
  脚注行（注：/备注/*）移入表级 footnotes。
- 跨页拼接：续页表头与当前表表头末尾一致视为重印并剔除后追加；无表头且列数一致才追加；
  表头不匹配或列数不同则拒绝拼接、划定新边界。每次拼接/拒绝的依据写入该表 `stitch_log`，
  `table_rebuild.explain_stitching()` 可导出说明。
- 空字符串 = 布局空白（blank），JSON null = 真实缺失（null），“—” = 披露占位（dash），
  三者分开标记；短行右侧补 null 并记 `missing_cols`（列缺失）。
- 数值支持千分位、负号/Unicode 负号、括号负数、% 后缀；单元格保留 `raw/num/unit/scale`
  （v = num × scale）与 `prov`（公告/来源/页码/页序/行/列），normalizer 之后仍可反查原文。
- 损坏页隔离进 `quarantine`，不关闭当前表、不污染同公告其他页与其他公告；
  修复后对该公告重跑流水线即可按键幂等重试。
- `pipeline.run(batch_size=N)` 支持分批提交：只影响故障时的落库粒度，
  不改变最终落库内容（各阶段为纯函数，upsert 幂等）。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。

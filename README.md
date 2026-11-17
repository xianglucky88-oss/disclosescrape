# disclosescrape — 抓披露平台公告（离线已抽取表格夹具），科目对齐与报告期归并、识别重复版本。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余已实现变换 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线 happy-path，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线、幂等和边界桩
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`subject_map`, `period_align`, `ann_page`, `table_rebuild`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 当前实现状态
- `subject_map.py`：`process()` 当前抛出 `NotImplementedError`，未接入基础 happy-path。
- `period_align.py`：`process()` 当前抛出 `NotImplementedError`，未接入基础 happy-path。
- `ann_page.py`：提供基础 `process()`，复制输入记录并补充 `_stage_ann_page` 阶段标记。
- `table_rebuild.py`：提供基础 `process()`，复制输入记录并补充 `_stage_table_rebuild` 阶段标记。

当前未实现阶段未进入基础流水线，因此 `make run` 仍可运行已接入的阶段。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。

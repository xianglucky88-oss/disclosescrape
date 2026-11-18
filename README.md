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

### ann_page：分页、版本与续采
沿用单记录入口 `ann_page.process(record)`，只做注解式变换；跨记录/跨重启的
列表发现、详情取得、版本确认状态由 `ann_page.AnnPageState` 管理，经 `store`
的独立 KV 表（`ann_page_state`）持久化，表格重建仍交给 `table_rebuild`。

- **来源身份不随游标变化**：`ann_identity` 只由来源站点 + 公告链接/来源编号
  派生（SHA256 截断，带 `ann:` 前缀），不含分页游标、批次、内容指纹；游标单独
  存于 KV 表。详情任务可由列表条目/`resume_scope()` 直接钉住 `ann_identity`。
- **重复链接 vs 页面更新**：同一身份在列表上再次出现是“重复链接”，只累计
  `list_hits/duplicate_links` 与刷新游标观测，不产生新版本；身份不变但详情
  内容指纹变化才是“页面更新”，版本号递增并追加 `versions`，记录原地覆盖。
- **抓取边界推进**：列表页请求成功（含空列表，空列表同时标记来源耗尽）才把
  游标推进到 `next_cursor`；列表页失败游标原地不动，重启后从同一页重试。
  详情页/附件失败与列表游标解耦：不回退、不阻塞推进，公告置 `detail_failed`
  进入下一轮 `pending()/resume_scope()` 续采，恢复成功后清空错误标记。
- **重试不重复写入**：详情记录以稳定身份为 `id`，经 keyer 生成固定键、store
  做 upsert；状态分片保存（批次/游标/公告），重试与重启只覆盖同键。
- **诊断**：`AnnPageState.diagnose(identity)` 返回状态、版本史、重复链接计数、
  错误原因与批次、来源游标，可定位到具体公告。
- **旧输入兼容**：无 `page_type` 的旧夹具走 legacy 路径，仅追加阶段标记，
  字段与键派生不变；旧库（仅 `records` 表）连接时自动补建状态表，旧行无损。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。

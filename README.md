# CiteWeave — 带有效时间的法规引用单机工作台

导入若干部法规文本及其通过 / 生效 / 失效日期，识别结构化条款与显式引用，
构建**带有效时间**的引用图。网页只是规则的可见入口：所有解析、决议、冲突
判定、时间图和导出逻辑都在服务端（`citeweave` 包），由持久化数据驱动。

## 安装与运行

```bash
python3 -m pip install -e '.[test]'
pytest -q
python3 -m citeweave --host 127.0.0.1 --port 5207
# 浏览器打开 http://127.0.0.1:5207
# 想直接载入示例数据：
python3 -m citeweave --host 127.0.0.1 --port 5207 --sample sample
```

数据库默认放在当前目录 `citeweave.sqlite3`（可用 `--db` 修改）。

## 功能对照

- **结构化条款与显式引用**：`parser.py` 按字符偏移识别 章/节/条/款/项，
  抽取 `第N条`、`第N条第M款第K项`、`前款`、`本条`、`前条`、`本章/本节`、
  `《其他法》第N条`；原文与所在原句、绝对偏移全部入库。
- **候选目标 + 选择依据**：解析（`resolver.py`）与决议分离。每次决议保留
  全部候选、分值、`basis` 依据；解析结果记录 `resolved / ambiguous /
  missing / repealed / cross_version_conflict` 及原因。同编号在多版本中
  指向不同文本时标 `cross_version_conflict`，**绝不用最新版替代历史版**。
- **时间图与时间线**：`GET /api/graph?date=YYYY-MM-DD` 只返回当天有效版本
  产生的边；目标缺失、目标已废止、版本不在效力期分别标记，并给出 BFS
  最短引用链（`chains`）。
- **日期对比**：`GET /api/diff?a=...&b=...` 输出边的新增、消失、重定向。
  边的跨版本身份是 `(文档代码, 条款号, 引用签名)`，签名对空白不敏感。
- **人工纠正与版本区间**：研究者可覆盖候选目标，或声明引用只适用于
  `[valid_from, valid_to)`；提交后受影响节点（同版本相对引用 + 候选命中
  目标版本/单元的引用）自动重新解析，决议版本号 `decision_rev` 递增。
- **研究快照**：快照可创建为 open，发布（published）后冻结。决议再变化
  不会改变已发布快照；发布本身走乐观版本号。
- **两人合并**：两个研究者从同一版本分别修正后创建合并会话；目标不同的
  引用在 `merge_items` 中**同时保留双方的目标、版本区间、说明、来源页和
  引文上下文**，合并接口不会以后保存者覆盖，未裁决分歧持续保留。
- **并发控制**：所有写操作基于版本号（`base_rev` / `revision`）。落后的
  写入返回 HTTP 409 与双方差异（`base` / `current`），不静默覆盖。
- **确定性导出**：`GET /api/export` 下载 ZIP，内含 `manifest`（解析器
  版本 `citeweave-parser/1.0.0` + 各文件 sha256）、versions（原文、原始
  指纹、归一化指纹）、citations、candidates、resolutions、anchors（定位
  锚点）。JSON 使用 `sort_keys`，ZIP 时间戳固定，字节可复现。
- **锚点失效判断**：`/api/anchors/verify` 用双指纹区分 `exact / reflowed
  / failed`——换行变化后归一化指纹仍相同（reflowed），内容改动则 failed。
- **崩溃恢复**：导入支持 `stage=staged`（只写原始版本行）与 `/finalize`
  两阶段。进程在两者之间被杀死后，重启时 `DB.recover()` 将遗留的 staged
  行标记为 `abandoned`（不产生半成品条款/引用），完成旧版本被拒绝，需重新
  导入。测试用子进程 + `os._exit(17)` 模拟 SIGKILL。

## HTTP API 摘要

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /api/imports` | 导入版本（可带 `idempotency_key`、`stage`） |
| `POST /api/versions/{id}/finalize` | 完成暂存导入 |
| `POST /api/versions/{id}/transition` | 生命周期跳转（非法返回 409） |
| `GET  /api/documents` · `/api/versions[?code=]` · `/api/versions/{id}/raw` | 浏览 |
| `GET  /api/graph?date=` · `/api/diff?a=&b=` | 时间图 / 对比 |
| `GET  /api/citations/{id}` | 原句、候选、选择依据、双方决定 |
| `POST /api/citations/{id}/decisions` | 纠正 / 版本区间（带 `base_rev`） |
| `POST/GET /api/snapshots[/{id}/publish]` | 研究快照 |
| `POST/GET /api/merges[/{id}/merge]` | 两人合并 |
| `GET  /api/export` · `/api/anchors/verify` · `/api/recovery` | 导出 / 锚点 / 恢复 |

## 架构取舍

- **标准库优先**：仅依赖 Python 标准库（SQLite、`http.server`），测试依赖
  pytest。单机单进程用一把进程内写锁串行化变更，SQLite `BEGIN IMMEDIATE`
  + WAL + `synchronous=FULL` 保证崩溃持久性；没有引入 ORM / Web 框架，
  规则代码可以不经过 HTTP 直接被测试调用。
- **解析与决议分离**：parser 只做结构和候选描述，resolver 做打分与选择。
  用户纠正后只需重跑受影响引用，并且每一步的 `basis` 可审计。
- **历史版本一等公民**：版本行存逐字原文，规范化（NFKC、空白折叠）只用于
  指纹/比对，从不回写原文；同一版本标签不同文本直接冲突，必须另存新版本。
- **冲突显式化**：同编号跨版本默认 `cross_version_conflict` 而非猜一个；
  合并分歧默认保留双方，而不是自动二选一。

## 限制（刻意的边界）

- 定位规则以常见中文行政法规格式为目标：条标题单独成行或“条号 + 同条
  正文”，款按非项内容行计数；复杂的编/章嵌套、表格、附件暂不支持。
- “前款/前条/本章”只在**同一版本**内解析，不跨版本回溯，避免隐式改写
  历史；跨文档引用按文档标题精确匹配，未做别名/简称推断。
- 时间粒度为“日”（ISO 日期字符串），效力区间为半开 `[effective, end)`。
- 单机工作台：无鉴权、无多进程写入协调；多研究者是数据层面的身份字段，
  不是账户系统。
- ZIP 导出为数据交换包，不含可独立打开的渲染页面；UI 数据只来自 API。

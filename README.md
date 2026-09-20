# CiteWeave

CiteWeave 是一个单机运行、带有效时间的法规引用工作台。服务端负责条款解析、候选生成、候选选择、状态机、冲突检测、时间投影和确定性导出；网页只是这些规则的可见入口，不在浏览器端实现判定。

## 快速开始

```bash
python3 -m pip install -e '.[test]'
pytest -q
python3 -m citeweave --host 127.0.0.1 --port 5207
```

打开：

```text
http://127.0.0.1:5207
```

默认数据库在 `.citeweave/citeweave.db`，SQLite 以 WAL 和 `synchronous=FULL` 运行。可通过 `--db path/to/file.db` 指定其他文件。

## 导入文本格式

解析器使用轻量 Markdown 风格文本：

```markdown
# 第一章 总则
第一条 为了规范活动，制定本法。
第二条 基本规则适用第一条。
第三条 特别事项适用第二条。
第四条 本章另有规定的除外。
第五条 补充事项适用前款。
```

导入接口也接受 `effective_date`、`repealed_date`、`version_label` 和初始状态。原始文本始终保存到 `documents.raw_text`；换行规范化后的结果只保存到 `normalized_text`，不会覆盖原文。

## 数据与规则模型

- `documents`：同一 `doc_key` 下的独立版本，含原文、原文 SHA-256、规范化文本、解析器版本、日期和 `lock_version`。
- `clauses`：章、条、款、项的结构化节点，保留节点原文、规范化指纹、位置键和跨版本稳定的 `semantic_key`。
- `citations`：保存原句、引文、字符位置、所在行、锚点指纹、候选结果、选择理由和引用锁版本。
- `candidates`：保存全部候选目标、分数、选择依据和是否精确层级匹配；人工选择的版本外目标也会显式记录。
- `decisions`：保存研究者修正、原始句子、来源定位、目标语义键、适用版本区间、说明、合并链和锁版本。
- `snapshots`：发布时冻结 JSON 载荷和指纹，之后修正引用不会改写已发布快照。
- `idempotency_keys`：显式 `Idempotency-Key` 请求头的响应会被复用；相同法规键和版本标签的重复导入也返回既有版本。

解析器当前识别：

- 显式编号：`第一条`、`第二条第三款`、`第三条第二项`。
- 位置引用：`前款`、`本章`。
- 跨版本候选：同一法规键、同编号条款会作为候选保留。同版本和层级精确匹配优先，未来生效版本扣分，避免用最新版自动替换历史版本。

## 有效时间

图中的每条边同时保留来源版本、目标版本、原句、引文和解析理由。查询 `/api/graph?date=YYYY-MM-DD` 时，服务端计算：

- `active`：来源和目标在该日期均可适用。
- `source_not_effective`、`target_not_effective`：尚未生效。
- `source_repealed`、`target_repealed`：已到废止日期。
- `source_superseded`、`target_superseded`：后一版本已经生效后，旧版本在该日期表现为被取代；在后一版本生效之前旧版仍按 active 展示。
- `target_missing`：没有候选目标。

被取代版本的边不会消失。例如 2021 版生效后，2020 年日期仍展示 2020 版指向 2020 版；2021 年日期则把 2020 版边标记为 `source_superseded`，而不是静默改指 2021 版。

`/api/compare?left=...&right=...` 按跨版本稳定引用键比较：

- `added`：右侧日期变有效或新出现。
- `disappeared`：左侧有效，右侧不再有效。
- `redirected`：同一条引用选择的子句 ID 改变。
- `changed_state`：目标未变但时间状态改变。

最短链接口 `/api/chain` 使用当天有效边做 BFS，并返回边及原句证据；找不到时返回 `found=false`，不会猜测路径。

## 人工修正、版本区间与合并

修正接口：

```http
POST /api/citations/{citation_id}/corrections
```

请求包含 `researcher`、`target_clause_id`、`expected_version`，并可选：

- `applies_from_version`
- `applies_to_version`
- `rationale`

同一研究者后续修正会把旧决策标成 `superseded` 并创建新决策。不同研究者选择不同目标时，落后写入返回 `409`，响应中包含当前记录、客户端请求和已有上下文，不做最后写入者覆盖。

可调用合并接口显式保留双方选择：

```http
POST /api/citations/{citation_id}/merge
```

合并后两个 active decision 都保留，引用状态变为 `divergent`；时间图的 `alternative_targets` 会包含双方研究者、说明、适用区间、来源定位和原句。

## 导出与锚点

`/api/export` 返回确定性 JSON 包：

- `parser_version`
- 文档原文、规范化文本和原文指纹
- 条款文本指纹与规范化指纹
- 引用定位锚点：行号、字符起止、locator、条款指纹
- 候选目标、分数、选择理由
- 研究者决策、版本区间和合并链
- 整个包的 SHA-256 `package_fingerprint`

决策的墙钟时间不进入导出包，保证相同数据在不同进程和时间导出一致。快照的发布时间在快照元数据中，不改变被冻结包的指纹。

`/api/anchors/verify` 可提交候选文本：

- 原文指纹一致：`valid`
- 原文不同但规范化文本一致：`wrapping_changed`，可继续用结构化 locator
- 规范化文本也不一致：`invalid`，锚点应重新定位

## HTTP API 摘要

- `GET /api/documents`，`POST /api/documents`
- `GET /api/documents/{id}`，`POST /api/documents/{id}/transition`
- `GET /api/citations`，`GET /api/citations/{id}`
- `POST /api/citations/{id}/corrections`
- `POST /api/citations/{id}/merge`
- `POST /api/citations/{id}/reparse`
- `GET /api/graph?date=...`
- `GET /api/compare?left=...&right=...`
- `GET /api/chain?source_clause_id=...&target_clause_id=...&date=...`
- `GET /api/timeline`
- `POST /api/snapshots`，`GET /api/snapshots/{label}`
- `GET /api/export`
- `POST /api/anchors/verify`

## 架构取舍

- **标准库优先**：只用 Python 标准库实现 HTTP、SQLite 和前端静态资源，降低单机安装和审计成本。
- **服务端规则集中**：浏览器不解析引用、不计算时间状态，只展示服务端返回的候选和理由。
- **版本即事实表**：每个导入版本独立保存，不做原地修订；同编号通过语义键关联而不是共享行。
- **证据优先于自动判断**：原句、候选、分数和理由都持久化，人工修正不会删除自动候选。
- **乐观锁而非锁页面**：文档和引用都有 `lock_version`，冲突响应携带双方差异，由研究者显式合并。
- **快照不可变**：快照写入冻结载荷，后续修正影响当前图但不重写研究快照。

## 限制

- 解析格式是受控的中文章/条/款/项文本，尚未覆盖所有立法排版、表格、附件、跨法规简称和脚注引用。
- 候选分数是确定性的规则权重，不是法律推理模型；复杂语义仍需人工确认。
- 版本区间按版本标签字典序比较，建议使用 ISO 日期标签。
- 单机服务默认监听本地地址，没有内建用户认证、TLS 或多节点复制。
- 大文件性能未做专门索引调优；当前重点是可验证的数据模型和正确的时间语义。

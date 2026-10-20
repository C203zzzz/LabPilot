# 架构

这份文档说明一次核价从资料进来到报告出去经过哪些环节、每一步留下什么、以及为什么有些价格会被拒收。所有字段名都能在 `backend/` 里找到定义，改动这些约定时请同步改这里。

## 分层

```text
backend/api/            HTTP 端点，只做参数校验和错误映射
backend/services/       业务逻辑：检索、抽取、判据、报告
backend/infrastructure/ 外部依赖适配：模型、检索通道、页面读取、用量账本
backend/repositories/   SQLite 持久化（JSON payload + 索引列）
backend/domain/         领域对象与不变量
backend/schemas/        序列化契约
frontend/               React 工作台，只做展示与文案翻译，不重算判据
```

数据落库只有一个仓库实现（`backend/repositories/sqlite.py`）。18 张表里，业务对象基本以 `(id, task_id, payload)` 的形状存放，payload 是完整 JSON，查询只用到 `task_id`。这样历史记录能整份读回，不会因为后来加了字段就读不出来。

## 一次核价的四个阶段

端点都在 `backend/api/procurement.py`，路径前缀 `/{task_id}`。

### 01 材料与报价

```text
POST   /intake                         写入本轮的材料、报价和数量
POST   /baseline/save | /baseline/confirm   确认比较基准
POST   /research-runs/{run_id}/start/price
```

上传的资料由 `backend/services/document_ingestion.py` 解析。每一页都记两项：`route` 是 `native_text` 或 `vision`，`route_reason` 说明为什么走这条路；`quality` 里放这一页读得怎么样，`provenance` 逐条记是谁读的（`pymupdf_native` / `docling` / `rapidocr` / 视觉模型）。

解析不出来不是"这一页没有文字"，而是三件分开记的事：`ParseWarning`（code + 人话 + 位置）、`ocr_status`（整份资料的进度）、以及页级 `route`。本地识别组件缺失时写 `local_ocr_unavailable`，不把空字符串当成"读完了"。

### 02 成交项目核价

```text
POST   /research-runs/{run_id}/start/price
```

一次 run 走两条检索线（lane）：`price` 和 `alternative`。价格线的产出是 `run['prices']`，每条价格都带自己的来源地址、公告日期、原文摘录和快照。

抓回来的页面正文按内容哈希存快照：`research_sources.snapshot_path()` 把响应体存成 `research-snapshots/<sha256>.bin`，来源上记 `{'sha256', 'path', 'size'}`。同一份内容重复抓取只落一次盘，报告里引用的 sha256 直接对应磁盘上那个文件。

### 03 其他品牌比较

```text
POST   /research-runs/{run_id}/start/alternative
POST   /research-runs/{run_id}/alternatives/{candidate_id}/price
```

候选只列各自资料里的配置原文和出处；没有出处的配置不进这张表。候选价格查询（`backend/services/alternative_price_lookup.py`）只把已落库的判据重新组织成界面能显示的形状，不重算任何一个判据，也不改变一次扫描的结论。

### 04 报告

```text
POST   /research-runs/{run_id}/report
GET    /research-runs/{run_id}/report
```

报告是 `sections` 列表，每节有 `key`、`title`、`summary`、`items`。`items` 里的每条证据都回到 `quote`、`url`、`publication_date` 这三样。

## 价格记录的字段

表格抽取在 `backend/services/procurement_tables.py`，`create_price_record()` 组装一条记录：

| 字段 | 含义 |
|---|---|
| `model` / `brand` | 要比的型号、品牌 |
| `amount` / `amount_unit` | 抽到的金额与单位 |
| `amount_scope` | `unit` 是单价，`single_item_total` 是单项合计 |
| `quantity` | 数量，读不到就是空 |
| `currency` | 币种，默认 `CNY` |
| `excerpt` | 这一行的原文 |
| `money_excerpt` | 把型号、单价、数量拼成的人类可读短句 |
| `table_evidence` | 表头、行、第几项、共几项、货币上下文 |
| `supplier` / `procurement_unit` / `project_id` | 供应商、采购单位、项目编号，读不到留空 |

`money_excerpt` 不是原文摘录，它是给人看的摘要；要核对时必须回到 `excerpt` 和快照。

## 判据与四个比较级别

`backend/services/procurement_rules.py` 的 `validate_price()` 是唯一的验收门。它给每条价格定一个 `level`：

| level | 什么情况 |
|---|---|
| `budget_reference` | 预算或最高限价，只作参考，不是成交价 |
| `current_comparable` | 官方挂牌价且是当天的，可比 |
| `historical_configuration` | 有成交/合同性质、日期落地、品牌已知、型号匹配，可比 |
| `model_reference` | 品牌或日期没核实，只能当线索 |

只有 `historical_configuration` 会被拿去做价格区间。区间还有两道刹车：配置口径没核实（缺 `configuration`）时不合区间，高低相差达到 `DISPERSION_REFUSAL_RATIO` 时也不合，两种都把单条记录列出来让读者自己看。

日期必须能在原文里按年月日匹配到，否则记 `date_verified=False`，级别的判定跟着降。

## 拒收与缺口怎么留痕

同一个"这条价格没用上"的事实有两个出口，分开写：

- `run['price_rejections']` 记被拒的候选和原因码，每种原因都能回到一条原文摘录和一个来源地址。原因码是固定的，由 `validate_price` 产出，`price_rejections.py` 只负责解释、不重新判：`body_unavailable`、`excerpt_not_in_source`、`not_awarded_candidate`、`model_mismatch`、`brand_mismatch`、`not_award_evidence`、`budget_not_achieved`、`amount_not_unit_price`、`unparsed_amount`、`total_not_unit_price`、`source_not_tender`、`unexplained`。
- `run['gaps']` 是给人看的缺口句子，不带异常类名。
- `run['failures']` 是给排查用的技术细节：`stage`、`type`（异常类名）、`code`、`location`（出错行）和脱敏后的 `error` 原文。页面上不出现。

三条规则在这个仓库里是硬约定：被拒的记录不删除，只标原因；一条坏链接只影响那一条，不升级成整轮失败；配置写错就响亮失败，不给兜底默认值。

## 前端边界

`frontend/src/services/` 下的模块只做三件事：把后端字段翻成中文句子、把价格按展示顺序排好、把长缺口列表折叠分组。判据、级别和"能不能给区间"都在后端算完之后才到前端。这意味着一处判据要改，只改后端和它的测试，不用在前端找第二份判断。

## 测试

`tests/` 覆盖后端。约定是：每条判据至少有一个测试钉住它的反例（价格被拒、缺口被写出），而不只是覆盖通过路径。`pip install -e ".[test]"` 之后 `python -m pytest tests -q` 能跑全套；解析层相关的两条 OCR 用例需要 `.[parsing]`，没装时按缺依赖跳过。

# 总体架构

## 1. 架构结论

采用“模块化单体 + 按需 akshare 采集器”的架构：

- `api` 是唯一的后端进程。原先还有一个 `worker` 消费事务性任务队列，它唯一的活是跑导入通道的作业；通道 2026-08-13 摘除后随之退役。
- PostgreSQL 承载业务数据与审计记录。事务性任务队列随导入通道一并删除。
- 业务数据归属于个人 Workspace；所有业务查询、写入、任务、文件对象和 AI 工具必须携带服务端解析的 `workspace_id`。
- 自动采集由独立 Python akshare 容器执行，按需拉起、跑完退出；容器输出标准化 CSV，由 `deploy/collector/load-*-direct.sql` 直接装载进宽表。
- Phase 5 自由价差由 Rust API 内的 `SpreadSeriesProvider` 读取；首发 `sanhe` 适配器仅代理 `DEC-042` 的三个三禾只读 POST 端点，自有 `self_hosted` 实现读取本库行情。
- PNG/SVG 由 ECharts 前端直接导出，不建设服务端图表渲染辅助进程。
- AI 通过应用层只读工具访问业务能力，不直接访问数据库。

“模块化单体”指领域与应用代码不拆分为网络服务；akshare 采集器是受网络白名单约束的按需数据获取进程，不承载平台领域逻辑。

## 2. 上下文图

```mermaid
flowchart TB
    U["用户浏览器"] -->|HTTPS| RP["Reverse Proxy"]
    RP --> FE["Vue Web"]
    RP --> API["Rust API"]
    API --> WC["Workspace Context"]
    API --> DB[("PostgreSQL")]
    API --> FS["ObjectStorage Port"]
    API --> JQ["Job Application Service"]
    JQ --> DB
    WK["Rust Worker"] --> DB
    WK --> FS
    SCH["盘后调度"] --> COL["akshare Collector（Python，按需）"]
    COL -->|公开接口与公开文件| EX["五家交易所"]
    COL -->|标准化 CSV + 导入 API| API
    API -->|服务端只读代理、限频与缓存| SANHE["三禾自由价差 API"]
    API --> AIG["AI Gateway"]
    AIG --> LLM["外部模型提供商"]
    FS --> LOCAL["本地文件适配器"]
```

反向代理固定采用 Nginx：`/` 提供 Vue 前端，`/api/` 转发 Axum API，`/events/` 支持 SSE。

## 3. 依赖方向

```mermaid
flowchart LR
    APIA["apps/api"] --> APP["application"]
    APP --> DOM["domain"]
    INF["infrastructure"] --> APP
    INF --> DOM
    DBAD["database adapter"] --> APP
    CNAD["connector adapter"] --> APP
    AIAD["ai provider adapter"] --> APP
```

约束：

- `domain` 不依赖 Axum、SQLx、akshare、文件系统或具体 AI SDK。
- `application` 定义用例和端口，只依赖 `domain`。
- `application` 的所有业务用例显式接收 `WorkspaceContext`，仓储方法不得省略 `workspace_id`。
- `infrastructure`、`database` 和各适配器实现端口。
- `apps/api` 只负责组合依赖、协议适配和生命周期。
- 业务模块不得直接拼接文件路径或执行任意 SQL。

## 4. 核心数据流

### 4.1 数据装载（直灌）

```mermaid
sequenceDiagram
    participant CR as cron
    participant CO as akshare collector
    participant FS as /opt/futures-platform/load
    participant DB as PostgreSQL
    CR->>CO: 按交易日拉起容器
    CO->>FS: 写标准化 CSV（失败只写 .csv.failed 标记）
    CR->>DB: \copy CSV → 临时表 → upsert 宽表
    DB-->>CR: 行数
```

原先这里是一条七层流水线：上传 → 暂存 → 逐行校验 → 冲突检测 → 人工确认 →
血缘 → canonical → 投影 → 宽表。它是为「人工上传文件、需要预览和回滚」设计的，
服务的是后来取消的 AI 分析功能；每日自动采集被迫走同一条路，代价是中间产物
（暂存行 1448 MB、变更记录 832 MB、血缘 292 MB）长得和业务数据一样大。
2026-08-13 整条摘除，见 `DEC-049`。

现在的约束：

- 采集失败**不写数据 CSV**，只留 `.csv.failed` 标记。写一个只有表头的空文件会被
  装载脚本当成「今天这个交易所一行都没有」而照单全收，把已在库的当日数据
  upsert 成空值。
- 装载脚本判定成败看**前后行数**，不看命令退出码：`\copy` 是 psql 元命令，
  不做变量插值，出错也不中断执行——首次试跑时五个 CSV 全报「装载成功」而库里
  一行没多，就是这么来的。
- 装载是幂等的：同一份 CSV 重装行数不变。日更每天两轮、补采还会重跑。
- 品种目录装载**不覆盖** `price_multiplier`：采集器给的 `contract_multiplier`
  是交易单位（鸡蛋 5），点值是 10，覆盖会让盈亏差一倍。

### 4.2 价差计算

`workspace-scoped market_price → contract/calendar normalization → price_basis/formula_version → aligned leg samples → spread_observation → statistics → chart`

缺失腿、时间不一致和来源冲突必须产生显式状态，不允许自动补值后伪装成原始数据。

- 时间点以 UTC/`timestamptz` 保存，交易日期使用交易所日历计算的 `trade_date`。
- 夜盘记录为独立 `session_type`，并归入交易所日历定义的下一交易日。
- 行情图默认 `price_basis=close`；日终持仓、未实现盈亏和权益默认 `price_basis=settlement`。
- MVP 不生成连续合约；外部连续合约通过来源与换月规则元数据进入。
- Phase 5A 的三禾序列没有原始腿价格，必须如实使用 `price_basis=upstream_spread`；
  按实际合约代码和本库交割月执行版本化散户可交易窗口裁剪后，再在服务端重算季节图
  与月度矩阵。详细边界见 `docs/phases/PHASE_05_SPREAD_ANALYTICS.md`。
- `SpreadSeriesProvider` 隔离 `sanhe` 与 `self_hosted`；UI 和响应始终披露真实 provider、
  source、取数时间、样本范围和算法版本。

### 4.3 akshare 自动采集

`盘后调度 → 按需启动 akshare collector → 交易所公开接口/文件 → 标准化 CSV → load-*-direct.sql → 宽表`

- 采集器是独立 Python 容器，每交易日盘后按需运行，完成后退出，不常驻。
- 默认只采 DCE、SHFE、CZCE：GFEX 与 CFFEX 采回来的行情在宽表里产出 0 行（本平台不跟踪其品种），却占掉 13% 的采集量，2026-08-13 起停采。东方财富按 `DEC-043` 承担席位兜底（排在全部官方源之后），并按 `DEC-045` 承担大商所的行情与合约目录——大商所官方接口自 2026-08-02 起全线 412，`DEC-041` 当初选的新浪兜底对其在市合约只覆盖 44%，两者均已退役，其余二手数据源不进入第一批。
- 数据获取路径固定为 akshare 封装的交易所公开接口与公开文件，不接受任意目标 URL。
- 采集范围为全市场日行情、全市场席位龙虎榜、交易日历和合约参数；历史回填策略遵循 `DEC-039`。
- 标准化 CSV 由装载脚本直接 upsert 进宽表；`source` 列记录来源，重复装载幂等。

### 4.4 AI 查询

`user session → WorkspaceContext → permission check → prompt policy → workspace-scoped read-only tool → provenance bundle → model explanation → audited answer`

从网页、文件和笔记读取的文本均视为不可信数据，不能覆盖系统工具策略。

## 6. 文件与数据库一致性

- 文件先以不可变对象写入，成功后再创建数据库引用。
- 对象元数据和对象键必须绑定 `workspace_id`，跨 Workspace 不得复用可猜测引用。
- 数据库事务失败时，无引用对象由清理任务按宽限期回收。
- 删除业务记录不立即删除原始文件；按保留策略标记并延迟回收。
- 备份必须同时覆盖 PostgreSQL、对象文件和主密钥材料。

## 7. 架构冲突与修正

| 编号 | 原方案问题 | 修正 |
| --- | --- | --- |
| `ARC-R01` | 阶段表先做交易持仓，结论又要求先做导入 | 开发计划统一为基础数据与导入优先 |
| `ARC-R02` | “模块化单体”与多个独立服务表述易混淆 | 领域单体；仅保留按需 akshare 采集器作为独立数据获取进程 |
| `ARC-R03` | 旧方案为浏览器采集和服务端图表导出设置高成本隔离进程 | 按 `DEC-031`、`DEC-040` 移除两者；采集改为 akshare，图表改为 ECharts 前端导出 |
| `ARC-R04` | 任意 URL + 自动发现无法稳定落地 | 固定五交易所白名单和 akshare 封装的公开接口/文件 |
| `ARC-R05` | PostgreSQL 年/月分区被当作默认方案 | MVP 先以索引和容量指标验证，分区设计待数据量证明 |
| `ARC-R06` | SSE 与 WebSocket 并列但职责不明 | 普通任务进度和 AI 流式回复使用 SSE；不再为采集引入 WebSocket |
| `ARC-R07` | `pgvector` 被提及但未列入已确认技术基线 | 结构化金融数据不使用向量检索；文档和交易笔记才使用 `pgvector` |
| `ARC-R08` | 手动导入确认规则直接套用自动采集会增加无意义操作 | `DEC-038` 允许白名单结构化自动批次跳过预览/确认，以失败隔离、回滚和质量警告兜底 |
| `ARC-R09` | 采集阶段过晚，套利开发缺少真实数据 | Phase 4 提前完成五交易所采集与全历史回填，Phase 5 再开发套利与图表 |

## 8. 主要质量属性

- 正确性优先于采集自动化率。
- Workspace 强制隔离优先于客户端传参便利性。
- 数据沿袭优先于缓存统计结果。
- 采集容器网络最小化优先于开放通用外部访问能力。
- 幂等和可恢复优先于追求“只执行一次”。
- 单机可部署，但不以牺牲备份、密钥和审计为代价。

### 8.1 资源结论

- 裁剪后，现有 1GB 内存、25G 磁盘的 VPS 支撑全部阶段；五个核心容器实测内存合计约 310MB。
- 唯一明确的磁盘增长点是席位全历史数据，估算 5–15GB；磁盘达到 80% 水位时扩容 VPS 磁盘，不更换机器。

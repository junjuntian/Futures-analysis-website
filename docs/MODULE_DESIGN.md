# 模块设计

## 1. 模块清单

| 模块 | 英文名称 | 职责 | 主要依赖 |
| --- | --- | --- | --- |
| Workspace | `workspace` | 个人 Workspace、当前上下文和租户边界 | `identity`、`audit` |
| 身份权限 | `identity` | 用户、角色、权限、session | `audit` |
| 审计 | `audit` | 写操作与安全事件记录 | 无业务模块依赖 |
| 基础目录 | `catalog` | Workspace 内交易所、品种、合约、版本化交易日历 | `workspace`、`audit` |
| 文件对象 | `storage` | 对象元数据与存储端口 | `audit` |
| 导入 | `import` | 文件识别、映射、预览、校验、提交、回滚 | `catalog`、`storage`、`jobs`、`audit` |
| 行情 | `market_data` | 标准化价格、来源和修订 | `catalog`、`import` |
| 套利 | `spread` | 多腿定义、公式版本、可插拔序列、散户窗口、价差与统计 | `catalog`、`market_data`、`jobs` |
| 交易复盘 | `portfolio` | 成交、交易组、持仓、权益和绩效 | `catalog`、`import` |
| 席位 | `seat_analysis` | 席位标准化、分类版本、汇总 | `catalog`、`import` |
| 图表 | `chart` | 图表模板、查询配置和前端导出安全约束 | `spread`、`portfolio`、`seat_analysis` |
| 数据连接器 | `connector` | 调度 akshare 采集、标准化输出、调用导入 API | `import`、`jobs` |
| AI | `ai` | 提供商适配、只读工具、对话与用量 | 各模块的查询端口、`audit` |
| 任务 | `jobs` | 入队、租约、重试、事件和死信 | PostgreSQL 适配器 |
| 报告 | `report` | 组合图表和分析产物 | `chart`、`storage`、`jobs` |

## 2. 依赖规则

- `identity` 不读取业务表来推断权限；权限由明确策略决定。
- 所有业务模块必须接收 `WorkspaceContext`，并把 `workspace_id` 传入仓储、任务、对象存储和审计。
- MVP 的 Workspace 是个人边界，不暴露邀请、共享或切换成员 API。
- `import` 是数据进入业务模块的统一批量入口，不在解析器中直接调用业务表 SQL。
- `market_data` 不依赖 `spread`；价差是行情的消费者。
- `spread` 只通过 `SpreadSeriesProvider` 获取规范化序列；`sanhe` 与 `self_hosted`
  适配器不得把各自字段泄漏到领域用例。三禾只读边界遵循 `DEC-042`。
- `portfolio` 与 `spread` 可共享 `catalog`，但不得互相引用内部实体。
- `chart` 只消费查询模型和图表数据 DTO，不修改来源数据。
- `ai` 只依赖应用查询端口，不依赖仓储实现。
- `connector` 只处理五交易所白名单和已实现的 akshare 连接器，不接受任意 URL；采集容器按需运行，跑完退出。
- `connector` 产出标准化 CSV 并调用 `import` API；白名单自动批次使用固定模板版本，免预览和人工确认，失败隔离、成功可整批回滚。手动文件导入流程不变。
- `audit` 接收脱敏事件，不反向依赖任何业务模块。

## 3. 分层职责

### `domain`

- 实体、值对象、领域规则和纯计算。
- 例：`SpreadDefinition`、`FormulaVersion`、`TradeGroup`、`Money`、`TradingDay`。
- 不处理 HTTP、数据库、文件、时钟或外部模型调用。

### `application`

- 用例、事务边界、权限检查、端口和 DTO。
- 例：`ResolveWorkspaceContext`、`ConfirmImport`、`RollbackImport`、`RecalculateSpread`、`QueryPortfolioPerformance`。
- 负责协调模块，禁止把 SQLx 类型暴露给领域层。

### `infrastructure`

- SQLx 仓储、本地/S3 存储、akshare 采集适配、AI Provider、时钟和加密实现。
- 适配器失败必须映射为稳定的应用错误。

### `apps`

- `apps/api`：HTTP、Cookie、OpenAPI、请求验证、限流和响应。
- `apps/worker`：任务租约、心跳、取消、重试和关闭。

## 4. 模块关键接口

这里只定义职责，不提供业务实现代码。

| 端口 | 关键操作 |
| --- | --- |
| `WorkspaceResolver` | `resolve_for_session` |
| `ObjectStorage` | `put`、`get`、`head`、`delete_after_retention`，全部携带 `workspace_id` |
| `JobQueue` | `enqueue`、`claim`、`heartbeat`、`complete`、`fail`，任务载荷绑定 `workspace_id` |
| `ImportParser` | `inspect`、`preview`、`stream_rows` |
| `DataConnector` | `schedule_collect`、`normalize_csv`、`submit_import` |
| `SpreadSeriesProvider` | `list_varieties`、`list_contract_months`、`load_series`；返回真实来源和取数元数据 |
| `AiProvider` | `capabilities`、`chat`、`stream` |
| `Clock` | `now` |
| `SecretCipher` | `encrypt_with_dek`、`decrypt_for_workspace`、`rewrap_dek`、`kek_version` |

## 5. 事务边界

| 用例 | 事务要求 |
| --- | --- |
| 确认导入 | 一个批次的正式数据、变更日志和状态原子提交；大文件可按可恢复分块策略执行 |
| 回滚导入 | 锁定批次；检查后续修改与依赖；有冲突则整批中止，否则按变更日志原子恢复；不支持部分回滚 |
| 重算价差 | 新建计算批次；不原地覆盖旧公式结果 |
| 修改席位分类 | 新建分类版本；历史版本保留 |
| 创建任务 | 业务状态变化与 outbox/job 记录同事务 |
| 保存 AI 对话 | 对话元数据与工具审计同事务；模型调用本身不在数据库事务内 |

## 6. Workspace 数据访问规则

- 仓储接口的业务查询条件必须包含 `workspace_id`，不得在调用端查询后再做内存过滤。
- 所有业务唯一约束把 `workspace_id` 作为第一组成部分。
- `workspace_id` 来源只能是服务端 session 解析结果或绑定 Workspace 的后台任务，不信任请求体中的自由值。
- 缓存键、对象键、幂等键、审计事件和 AI 来源引用必须包含 Workspace 维度。
- 跨 Workspace 数据访问属于安全事件，即使底层主键真实存在也返回不可见。

## 7. 错误分类

| 类别 | 示例 | 行为 |
| --- | --- | --- |
| `validation_error` | 字段格式、日期、系数无效 | 不重试，返回字段错误 |
| `conflict` | 唯一键冲突、版本冲突 | 按显式冲突策略处理 |
| `permission_denied` | 越权访问 | 拒绝并记录安全事件 |
| `dependency_unavailable` | 交易所接口、akshare 或 AI Provider 不可用 | 可退避重试 |
| `rate_limited` | 外部站点或模型限流 | 按 `retry_after` 重试 |
| `data_quality_error` | 自动采集质量警告、腿价格缺失 | 自动采集只记录警告、不拦截；分析侧显示质量状态 |
| `internal_error` | 未分类错误 | 脱敏返回，保留关联 ID |

## 8. 可拆分条件

第一版不拆业务微服务。只有满足以下任一证据后才评估拆分：

- akshare 采集负载已成为 API/Worker 的稳定性瓶颈，且按需容器隔离仍不足以消除影响。
- 单模块需要独立伸缩且数据库事务边界已稳定。
- 部署、故障和团队所有权收益大于网络与一致性成本。

拆分不得改变领域标识、API 契约和数据沿袭语义。

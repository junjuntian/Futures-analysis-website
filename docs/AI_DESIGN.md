# AI 设计

## 1. 定位

AI 模块用于解释、归纳和辅助查询，不负责生成或修改确定性业务数据。胜率、价差、百分位、回撤和费用等结果由领域代码与 SQL 计算。

第一版 AI 不具备写数据库、删除数据、修改权限、发起交易或执行任意 SQL/代码的能力。

AI 对话、工具调用、用量和来源归属于 Workspace。工具服务端从当前 session 解析 `WorkspaceContext`，不得接受模型或客户端自由指定的 `workspace_id`。

## 2. Provider 网关

统一适配 OpenAI、Grok、Gemini，但不假设三家能力完全一致。每个模型记录能力矩阵：

- `supports_tool_calling`
- `supports_structured_output`
- `supports_streaming`
- `supports_vision`
- `max_context_tokens`
- `data_retention_policy`
- `region`

应用只使用已探测且批准的能力。Provider 返回值先规范化，再进入对话和审计层。

默认提供商、允许模型、数据传输区域和费用上限仍待确认；无论 Provider 如何选择，AI 默认只读，流式输出使用 SSE。

## 3. 只读工具

| 工具 | 作用 | 强制限制 |
| --- | --- | --- |
| `get_portfolio_summary` | 持仓、权益、浮盈亏 | 账户权限、日期范围、字段白名单 |
| `get_trade_statistics` | 胜率、盈亏比、回撤 | 返回统计口径和样本数 |
| `get_spread_series` | 价差序列 | 限制点数，返回公式版本 |
| `get_spread_statistics` | 百分位、季节性 | 返回窗口、排除规则 |
| `get_seat_statistics` | 席位汇总 | 返回分类版本和可信度 |
| `get_source_lineage` | 来源和批次 | 不返回敏感路径或密钥 |
| `create_chart_preview` | 生成图表查询定义 | 不写正式数据 |

不提供 `execute_sql`、`read_file`、`fetch_url` 或通用 shell 工具。

所有工具的隐藏强制参数包含当前 `workspace_id`；数据库仓储在查询层执行隔离，不依赖模型参数或结果后过滤。

## 4. 调用流程

1. 校验用户 session，解析唯一个人 Workspace、角色和资源范围。
2. 分类问题并选择允许的工具集合。
3. 将用户输入与不可信引用内容分离。
4. 模型提出结构化工具调用。
5. 服务端按 JSON schema、权限、行数和日期范围重新校验。
6. 调用 Workspace 范围内的应用查询服务并生成 `provenance_bundle`。
7. 模型基于受控结果生成解释。
8. 响应附数据截止日期、样本区间、排除规则、来源和免责声明。
9. 保存脱敏的工具审计与用量。

## 5. 来源模型

每个工具结果返回：

- `data_cutoff_at`
- `sample_from`
- `sample_to`
- `sample_count`
- `formula_version_id` 或 `classification_version_id`
- `workspace_id` 的不可外泄内部绑定
- `price_basis`
- `calendar_version_id`
- `exclusion_rules`
- `source_refs`
- `calculation_batch_id`

AI 回答中的数值必须能映射到工具结果。模型自行计算的数值不得冒充系统统计。

## 6. 提示词注入防护

- 网页、文件、OCR、笔记和数据库文本均标记为“不可信数据”。
- 系统策略和工具清单由服务端固定，不从检索内容中读取。
- 引用文本中的“忽略规则”“调用工具”等指令按普通数据处理。
- 工具参数必须通过服务端 schema；模型不能扩大日期、账户或资源范围。
- 模型即使猜中其他 Workspace 的 UUID/BIGINT，也不能获得该资源存在性的差异响应。
- 工具结果限制行数和字段，防止上下文泄漏。
- 输出经过 Markdown/HTML 安全渲染，链接与 SVG 不被直接信任。
- 对敏感操作的诱导只返回拒绝和允许的只读替代方案。

## 7. 密钥与配置

- API Key 通过 `SecretCipher` 使用信封加密，保存 `wrapped_dek` 与 `kek_version`，并绑定 Workspace 和用途。
- 前端保存后只显示掩码、创建时间和最后测试状态。
- Provider 测试接口只返回连通性和能力，不回显凭据。
- 日志、错误、Tracing 和审计参数统一脱敏。
- Base URL 必须经过管理员批准和 SSRF 校验，不允许普通用户任意配置。

## 8. 对话与保留

- 对话、消息、工具调用和用量分表记录。
- 提示模板和工具 schema 版本化。
- 用户可见历史与审计日志的保留目的不同，不混用。
- 对话保留周期、删除策略和是否允许导出待确认。
- 不把整个数据库或完整原始文件发送给外部模型。

## 9. 语义检索

结构化金融数据通过受控 SQL 查询工具获取，不使用向量检索替代确定性查询。文档和交易笔记允许使用 `pgvector`，但不得把整库数据直接发送给模型。

## 10. 成本与稳定性

- 按用户、Provider、模型记录 token、费用、延迟、超时和错误。
- 支持用户级和系统级预算、最大输出长度、并发和频率限制。
- Provider 失败不自动切换到其他 Provider，除非用户批准数据传输策略和回退规则。
- 工具调用和模型请求使用不同超时；重试只针对安全、幂等错误。
- 流式响应中断时记录未完成状态，不将半截回答标记为成功。

## 11. 评估与验收

建立固定评估集：

- 数值必须与工具结果一致。
- 回答必须含截止日期、样本区间和来源。
- 回答涉及行情、价差、持仓或盈亏时必须显示 `price_basis`。
- 越权账户、日期或字段请求被拒绝。
- 跨 Workspace 资源标识、对话、工具调用和来源查询全部按不可见处理。
- 提示词注入样例不能获得写权限、任意 SQL 或秘密。
- Provider 不支持工具调用时必须明确降级，不伪造结果。
- 费用和 token 记录与 Provider 账单允许误差范围一致。

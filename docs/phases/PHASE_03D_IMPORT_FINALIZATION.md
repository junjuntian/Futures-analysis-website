# Phase 3D：导入收口、原子回滚与补偿

## 1. 授权状态

**当前状态：用户已确认范围与关键决策，Phase 3D 已获实施授权；Generator 尚未开始。**

本文是 Planner 交付给 Generator 的唯一 Phase 3D 实施契约。本次 Planner 更新只修改规划文档，不编写业务代码、不创建或执行数据库迁移，也不部署到 `futures` VPS；主 Agent 可在本次规划提交完成后按第 11 节任务包依次调用 Generator。

准入基线：

- 开发分支为 `phase/03-import-foundation`。
- Phase 3A、3B、3C 均已完成并经独立 Evaluator PASS。
- Phase 3C 实现提交为 `04011ed`，收口提交为 `6e1d46d`。
- Phase 3D 云端准入基线为提交 `636c8ae036f6ea65e8292bca19f38205db98f4a6`：GitHub Actions CI run `30187416767` 成功，Container images run `30187946869` 成功。
- Phase 3D 必须在上述提交均为当前 HEAD 祖先、工作树干净后开始。
- 上述云端 run 只证明实施前基线；Phase 3D 新提交必须重新通过 CI 和容器镜像工作流，不得复用旧 run 作为退出证据。

## 2. 目标与范围

Phase 3D 是 Phase 3 的最终收口阶段，目标是让导入批次从上传到回滚或补偿形成完整、确定、可审计、可追溯的闭环。

授权后计划实现：

1. 为 Phase 3C 的正式写入同步生成完整、顺序稳定的 `import_row_changes`。
2. 对成功批次执行全批次回滚预检，检测目标记录的后续修改和下游依赖。
3. 仅在全部预检通过时，在一个数据库事务中按逆序执行整批回滚；任一冲突时正式数据零变更。
4. 返回稳定、完整、可分页复查的回滚冲突清单，不提供部分回滚。
5. 通过 `compensates_batch_id` 建立补偿批次，补偿批次复用上传、映射、预览、确认、幂等、Worker 和审计流程。
6. 建立导入、回滚、补偿之间的审计链和数据来源链。
7. 治理对象存储孤儿、数据库记录缺对象、对象哈希或大小不一致等异常。
8. 完成导入中心前端的上传、inspect、mapping、preview、confirm、SSE、错误、批次详情、回滚、补偿、冲突提示和确认流程。
9. 收口 Phase 3A、3B、3C 的非阻断 MEDIUM，并形成可重复的 API、数据库、RLS、前端和 E2E 自动化。
10. 在 `futures` VPS 完成 Phase 3 全量验收，再由独立 Evaluator 审查至 PASS。

## 3. 明确非目标

本阶段不得实现或预建：

- 套利统计、套利图表或其他业务图表。
- 交易、成交纠错、持仓或交易组。
- 席位分析。
- 外部网站采集、交易所连接器或任意 URL 抓取。
- 外部数据自动采集。
- AI。
- 自动回测。
- 任何行情、交易、套利等正式业务域表。
- 部分回滚、按行勾选回滚或“忽略冲突继续回滚”。
- 自动部署 `futures` VPS。

`cancel`、人工 dead-letter replay 和冲突候选人工合并已经由用户明确排除，Phase 3D 不得实现或预建。

## 4. 遗留 MEDIUM 归属

### 4.1 Phase 3A

| 项目 | 归属与 Phase 3D 处理 |
| --- | --- |
| multipart OpenAPI schema | 已在 Phase 3B 关闭；3D 仅做全量契约回归，不重复实现。 |
| 对象文件与数据库事务之间的孤儿治理 | 纳入 3D：持久化扫描结果、故障注入、对象与数据库一致性检查、quarantine 和审计；禁止物理删除。 |
| 可重复 API/数据库/RLS 自动集成测试 | 纳入 3D：与回滚、补偿、对象治理一起形成完整自动化矩阵。 |

Phase 3A 的 LOW 响应 envelope 风格不作为 3D 范围扩张理由；只要求本阶段新增接口与项目现有 envelope 保持一致。

### 4.2 Phase 3B

Phase 3B 的 5 项 MEDIUM 已由 Phase 3C 全部关闭：inspect 前端状态、errors 游标分页、映射失败事务回滚、模板 `dataset_type` 冻结竞争和脚本迁移前置说明。3D 只运行回归，不重复排期。

### 4.3 Phase 3C

| 项目 | Phase 3D 计划 |
| --- | --- |
| SSE OpenAPI 未直接表达逐帧事件 schema | 固化事件 discriminated schema、`text/event-stream` 响应文档、header 和稳定错误契约，并增加 OpenAPI 快照/契约测试。 |
| Worker 缺少跨 Workspace 显式公平轮询 | 设计可证明无单 Workspace 长期饥饿的轮询/配额策略；保留 `SKIP LOCKED`、租约和 generation fence 不变量，并做持续负载测试。 |
| SSE 建流后不周期性重验 Session/权限 | 增加有限周期重验或等价的服务端撤权终止机制；失效后停止推送、写脱敏审计，不泄露其他 Workspace 事件。 |
| 确认面板和断线重连缺少组件级交互测试 | 随完整导入前端补齐确认、断线、重放、终态、错误、回滚与冲突确认组件测试。 |

## 5. 数据模型计划

以下是已授权 Generator 的模型契约；具体迁移版本号由 Generator 按仓库顺序生成，且只能创建前向迁移，不得改写已执行迁移。

### 5.1 `import_row_changes`

每次正式变更必须写一条不可变记录，至少包含：

- `id`、`workspace_id`、`batch_id`、`sequence_no`。
- `target_kind` 与受控的 `target_id`；不得接受客户端表名、SQL、列名或表达式。
- `operation`：仅允许 `insert`、`update`、`soft_delete`。
- `before_json`、`after_json`。
- 写入完成后的 `target_row_version`。
- `source_file_id`、`source_row_number`，保证行级来源可追溯。
- `created_at`。

约束：

- `(workspace_id, batch_id, sequence_no)` 唯一，`sequence_no` 在一个批次内连续且确定。
- `workspace_id` 必须与批次、目标记录、源文件一致。
- 正式目标、批次终态、事件、审计和变更记录在同一事务提交。
- 变更记录不可更新或删除；回滚只追加回滚审计，不篡改原变更。
- `overwrite` 必须保留完整、脱敏边界内的 `before_json`，并记录写入后的版本。

### 5.2 回滚能力版本

- `import_batches` 必须增加 `rollback_capability`、`change_log_version` 或语义等价的不可歧义能力标记。
- 只有 Phase 3D 正式写入路径在同一事务中生成完整 `import_row_changes` 的批次，才可标记为可直接回滚。
- Phase 3C 已成功批次没有完整 change log，尤其无法可靠还原 `overwrite` 前值；禁止根据当前正式记录、审计摘要或 staging 数据伪造 backfill。
- 旧批次必须稳定返回 `rollback_not_available`，前端不得展示可执行回滚按钮；若需纠错，只允许创建带 `compensates_batch_id` 的可追溯补偿批次。
- 能力标记一经批次成功提交不可提升或回退；迁移只能把既有批次显式标记为不可直接回滚。

### 5.3 回滚请求、冲突和幂等

计划增加持久化的回滚请求/尝试和冲突明细模型，逻辑上至少保存：

- Workspace、原批次、发起人、幂等键摘要、状态和时间。
- 预检基线/指纹、冲突总数、完成结果。
- 冲突类型、目标 ID、期望版本、当前版本、依赖类型和脱敏说明。
- 与审计事件和批次事件的关联 ID。

不得保存明文 `Idempotency-Key`。冲突列表必须稳定排序并支持游标分页；重试同键同参返回原结果，同键异参返回稳定 `409`。

### 5.4 补偿批次

- `import_batches.compensates_batch_id` 指向同 Workspace 的已结束原批次。
- 一个补偿批次只能直接补偿一个批次；链路允许多层，但禁止自引用和循环。
- 创建补偿草稿不修改原批次或正式数据。
- 补偿必须经过完整 preview、validate、confirm 和 Worker 流程，不能绕过冲突策略或权限。
- 批次详情返回可遍历的直接前驱/后继摘要；链路查询必须有深度上限并检测循环。
- 原批次存在后续修改、其他批次覆盖或下游依赖时，直接回滚必须被拒绝；补偿批次是唯一允许的纠错入口。

### 5.5 来源与批次可追溯

任一 `imported_records` 记录必须能只读追溯到：

`imported_record → source_import_batch → import_row_change → source_file/source_row → stored_object(SHA-256) → mapping_version → confirmation/job → actor/audit`

回滚后仍保留原始文件、文件元数据、映射版本、staging/error 报告、变更日志、事件和审计。对象生命周期清理不得破坏法定/产品保留期内的追溯链。

## 6. 回滚 API 计划

授权后新增或完善：

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| `POST` | `/api/v1/imports/{import_id}/rollback-check` | 同步只读全量预检；返回预检指纹、能力标记、可回滚性、完整冲突计数和首屏冲突。 |
| `GET` | `/api/v1/imports/{import_id}/rollback-conflicts` | 按稳定游标分页读取最近一次指定预检的冲突。 |
| `POST` | `/api/v1/imports/{import_id}/rollback` | 同步重新执行全量预检；通过后幂等创建唯一 `import_rollback` job 并返回 `202`，不在 API 请求内修改正式数据。 |
| `POST` | `/api/v1/imports/{import_id}/compensations` | 创建引用原批次的补偿草稿；后续复用现有 mapping/preview/confirm API。 |
| `GET` | `/api/v1/imports/{import_id}/lineage` | 返回批次、文件、对象、映射、确认、补偿与回滚链的脱敏摘要。 |

所有有副作用接口要求 Session、写权限、Origin、CSRF 和幂等校验。跨 Workspace 统一不可见；权限拒绝和业务拒绝均形成脱敏审计。

稳定错误至少包括：

- `rollback_not_allowed`
- `rollback_not_available`
- `rollback_precondition_stale`
- `rollback_conflict`
- `rollback_already_completed`
- `rollback_in_progress`
- `rollback_idempotency_key_reused`
- `compensation_not_allowed`
- `compensation_cycle`
- `object_consistency_error`

OpenAPI 必须同步请求/响应 envelope、header、状态码、游标、错误枚举和 SSE 帧 schema。

## 7. 冲突、锁序与原子性不变量

### 7.1 回滚预检

预检在一致性快照中读取批次全部变更，至少检测：

- 批次 `rollback_capability`/`change_log_version` 不支持直接回滚。
- 当前目标不存在、已软删除状态不符或 Workspace 不符。
- 当前 `row_version` 不等于导入完成时记录的版本。
- 当前值与 `after_json` 的受控比较不一致。
- 目标记录存在后续人工修改、其他导入批次覆盖或补偿。
- 存在受控依赖检查器报告的下游引用。
- 变更日志缺失、序号缺口、重复目标/非法操作或来源链断裂。

冲突检测器使用服务端受控枚举，不允许动态 SQL 或客户端声明依赖。

`POST /rollback` 不得盲目信任先前 `/rollback-check` 的结果。它必须同步重新执行相同的全量预检，并在单个短事务中完成 Workspace/幂等 advisory lock、批次锁、请求指纹校验、唯一 rollback request、唯一 `job_type=import_rollback` 任务、queued 事件和脱敏审计。任一步失败不得留下已排队但无请求记录的任务；同键同参返回原任务，同键异参返回稳定 `409`。

### 7.2 执行回滚

- 仅 `succeeded`、能力标记允许且未回滚的批次可入队。
- Worker 异步领取 `job_type=import_rollback`；沿用 `SKIP LOCKED`、租约、续租、`lease_generation` fence、重试和第五次 dead-letter 不变量。
- Worker 的执行事务必须重新锁定任务、批次和全部受影响目标，再重复全量预检；API 预检结果不能替代事务内检查。
- 固定锁序：job → batch → rollback request → 按受控目标类型和目标 ID 排序的目标行；不得破坏 Phase 3C 已验证的 job → batch 顺序。
- 全部通过后，按 `sequence_no DESC` 逆序恢复。
- `insert` 仅删除仍保持导入后版本且无依赖的记录。
- `update` 仅在版本匹配时恢复 `before_json`，并以新的版本表达回滚动作。
- `soft_delete` 恢复删除前状态。
- 任一检查或写入失败，禁止提交任何目标逆变更；不允许已成功的子集落库。
- 成功时全部目标逆变更、受影响数据范围的失效记录、批次 `rolled_back`/`rolled_back_at`、rollback request 和 job 终态、最终事件及审计在一个数据库事务中原子提交。
- Worker 重验发现冲突时，全部目标数据保持零变更；完整冲突快照、批次 `rollback_conflict`、rollback request/job 终态、冲突事件和审计在一个数据库事务中提交。
- 数据失效只形成导入域受控失效/outbox 记录，不实现套利、行情、交易、图表或重算业务。
- 同批次并发回滚只允许一个执行；其余请求幂等重放或稳定冲突。

### 7.3 冲突零变更证明

自动化测试必须在事务前后对以下集合计算并比较摘要：

- 全部目标记录及版本。
- 原 `import_row_changes`。
- 批次终态、正式结果计数。
- 补偿链。

发现任一冲突时，除持久化的冲突报告、状态、事件和审计外，上述目标业务集合必须逐字节等价；不得短暂提交后再补救。存在后续修改或依赖时，响应和前端只提供补偿批次入口。

## 8. 对象存储一致性治理

### 8.1 检查类别

- 数据库有 `stored_objects`，对象文件不存在。
- 对象存在但数据库无记录。
- 大小、SHA-256、backend、state 或 Workspace 路径不一致。
- `.tmp` 或 `pending` 对象超过阈值。
- 数据库提交不确定或进程中断留下的待处理对象。
- 已被 `import_files` 引用的对象被错误标记为孤儿或可 quarantine。

### 8.2 治理原则

- 扫描只允许在配置的对象存储根目录内运行，拒绝路径穿越和符号链接越界。
- 默认只扫描和报告；quarantine 必须是独立、显式、可审计、幂等且可复核的受控动作。
- 有数据库引用、保留期未到、哈希未确认或状态不明的对象不得 quarantine。
- 状态演进仅允许现有状态到 `quarantined` 的受控转换；Phase 3D 不新增 `deleted` 状态。
- 所有扫描结果按 Workspace 隔离；普通用户不得获知其他 Workspace 的对象键。
- 对象扫描和 quarantine 任务使用受控 job type，不允许 payload 注入路径。
- Phase 3D 绝不物理删除对象，不提供删除 API、自动清理任务、定时删除或管理员物理删除后门；quarantine 中对象保留到后续独立阶段另行决策。

### 8.3 自动化故障注入

必须覆盖 rename 后数据库失败、commit 响应不确定、进程中断、对象缺失、哈希错误、孤儿对象、过期临时文件、重复扫描和重复 quarantine。测试使用隔离临时对象根，不触碰 VPS 真实业务对象；测试断言任何路径均未物理删除对象。

## 9. 前端完整流程

导入中心计划形成以下用户流程：

1. 上传文件并显示大小、SHA-256、状态与失败原因。
2. inspect 参数确认：编码、分隔符、工作表、表头。
3. mapping 编辑/模板选择及预览失效提示。
4. preview 展示原始值、规范值、错误、警告、重复和冲突计数。
5. confirm 页面展示冻结参数、冲突策略、预计影响和二次确认。
6. 任务进度页使用 SSE，支持 `Last-Event-ID` 重放、断线退避重连、Session/权限失效和终态收敛。
7. errors 游标分页、可理解的稳定错误提示和重试入口。
8. 批次详情展示来源、文件、映射、确认人、任务、审计摘要和 lineage。
9. rollback-check 页面展示影响数、冲突总数、分页清单和“整批零变更”语义。
10. rollback 确认页要求明确二次确认；执行中禁用重复提交；结果页区分成功、陈旧预检、冲突和系统失败。
11. compensation 向导从原批次创建草稿，明确“补偿是新批次，不改写原审计链”。

前端不得接受或显示明文 Cookie、Token、幂等键、数据库凭据、原始对象绝对路径或跨 Workspace 标识。

组件级测试必须覆盖：确认面板、冲突策略、重复提交、SSE 断线/重放、撤权终止、回滚二次确认、冲突分页、陈旧预检、补偿链接和失败恢复。

## 10. Worker 公平性与 SSE 收口

- Worker 继续使用 PostgreSQL `FOR UPDATE SKIP LOCKED`、租约、续租、最多 5 次重试、dead-letter 和 `lease_generation` fence。
- 公平策略必须以 Workspace 为调度单位，给出确定排序/游标或受控配额；测试证明一个持续灌入的 Workspace 不能让另一个 Workspace 永久饥饿。
- 不降低 Phase 3C 已验证的 job → batch 锁序和旧代不可提交不变量。
- SSE OpenAPI 明确每种事件帧；事件 payload 继续禁止原始行、`record_data`、Cookie、Token、CSRF 和明文幂等键。
- 长连接周期性重验间隔必须可配置并有上限；会话失效、成员撤权或 Workspace 不可见时终止流并形成脱敏审计。

## 11. Generator 实现任务包

各任务包按顺序实施和验证。Generator 不得跨包提前实现排除项；每包完成后先运行相应小门禁并报告差异，再进入下一包。

### 11.1 契约与迁移

- 同步 Domain 状态、稳定错误、OpenAPI 草案和前向迁移。
- 增加 `import_row_changes`、rollback request/conflict、受控失效记录、对象一致性 run/finding/quarantine 元数据及必要的批次能力字段。
- 新表从创建时包含 `workspace_id`、索引、外键、强制 RLS 和最小权限。
- 既有 Phase 3C 成功批次显式标记为不可直接回滚；禁止伪造 change log backfill。

### 11.2 正式导入变更日志

- 扩展 `import_confirm` Worker，使 insert/overwrite 的正式数据、`import_row_changes`、批次/job 终态、事件和审计同事务提交。
- 保证 sequence、before/after、来源文件/行、版本和能力标记完整。
- `skip`/`keep_conflict` 未修改正式记录时不得伪造 change row。

### 11.3 回滚预检与幂等入队 API

- 实现 `/rollback-check`、冲突游标分页和 `/rollback`。
- `/rollback` 同步重新全量预检，仅通过时幂等创建唯一 `import_rollback` job，返回 `202`。
- 覆盖同键同参、同键异参、多连接并发、旧批次不可回滚和跨 Workspace 不可见。

### 11.4 回滚 Worker

- 增加 `import_rollback` 受控执行器，复用租约、重试、generation fence 和 Workspace 公平调度。
- Worker 事务内重验、逆序恢复、冲突零变更，并原子提交数据失效、批次/request/job 终态、事件和审计。
- 以故障注入验证提交前退出、提交后重领、瞬时重试、永久失败和第五次 dead-letter。

### 11.5 补偿与 lineage

- 实现补偿草稿、同 Workspace/无循环约束和 lineage 查询。
- 补偿批次复用完整上传至确认流程。
- 后续修改或依赖冲突的界面与 API 只提供补偿入口。

### 11.6 对象一致性

- 实现只读 scan/check、持久化 finding、显式 quarantine 和审计。
- 验证路径根、符号链接、Workspace 隔离、重复扫描/quarantine 和进程中断恢复。
- 代码、路由、任务和测试中不得存在物理删除能力。

### 11.7 前端和 Phase 3C MEDIUM

- 完成导入批次主流程、回滚预检/确认/进度、冲突分页、补偿和 lineage。
- 收口 SSE OpenAPI 帧 schema、Workspace 公平轮询、SSE 周期性权限重验、确认/断线重连组件测试。

### 11.8 全量门禁、VPS、Evaluator 与收口

- 运行第 12、13 节全部本地、CI、数据库、RLS、前端和对象测试。
- 用 workflow dispatch 为候选 SHA 构建不可变 GHCR 镜像，记录三个 digest。
- 在 `futures` VPS 备份后按 digest pull、迁移、部署并执行 `PHASE3D_E2E_PASS`。
- 独立 Evaluator 复核至 PASS，再按第 15 节完成合并和标签。

## 12. 自动化测试矩阵

### 12.1 Rust 单元与服务集成

- 变更日志序号、before/after、来源行和版本记录。
- insert/update/soft-delete 逆序算法。
- 所有状态转换和稳定错误映射。
- rollback/compensation 权限、CSRF、Origin 和幂等。
- OpenAPI rollback/compensation/SSE schema 契约。
- 对象一致性扫描的路径安全、分类和幂等。

### 12.2 PostgreSQL 与 RLS

- 新增表全部包含 `workspace_id`、索引、外键、强制 RLS。
- `futures_runtime` 保持非 superuser、无 `BYPASSRLS`。
- 跨 Workspace SELECT/INSERT/UPDATE/DELETE 全部拒绝。
- 变更日志不可变、序号唯一、补偿同 Workspace、禁止循环。
- 无冲突回滚成功；后续修改、后续导入覆盖、删除、依赖、版本不符、变更日志损坏均整批零变更。
- 两连接/多连接并发回滚、回滚与新修改竞争、回滚与补偿竞争无死锁且结果确定。

### 12.3 API

- rollback-check、conflicts 分页、rollback 幂等入队、compensation、lineage 正常/边界/恶意输入。
- 同键同参、同键异参和 20 路并发回滚。
- 跨 Workspace 统一不可见；viewer/未知角色拒绝。
- 预检后发生修改返回 `rollback_precondition_stale` 或完整冲突，业务零变更。
- 审计覆盖成功、失败、拒绝、幂等重放和冲突。

### 12.4 前端

- 上传到成功导入的主流程。
- inspect/mapping 变更后的状态失效。
- confirm、SSE 断线重连、errors 分页、终态。
- rollback-check、二次确认、冲突/陈旧预检、幂等重复点击。
- 补偿草稿与 lineage。
- Session/权限失效和 API 错误恢复。

### 12.5 对象一致性与安全

- 第 8.3 节全部故障注入。
- 扫描不得越出对象根目录，不跟随恶意符号链接。
- scan/check/quarantine 全路径断言对象物理删除数为 0。
- 日志、响应、事件、审计、错误报告和测试证据秘密扫描命中为 0。

### 12.6 全量回归

- Phase 1/2 认证、Session、Workspace、CSRF、审计和 RLS 回归。
- Phase 3A/3B/3C 的 TXT/CSV/XLS/XLSX、inspect、mapping、preview、四策略、Worker、SSE、幂等与并发回归。
- 现有 Phase 3B 5 项 MEDIUM 关闭证据继续通过。

## 13. 本地、CI、VPS 与 Evaluator 门禁

### 13.1 本地与 GitHub CI

授权实现后至少运行：

```powershell
git diff --check
Set-Location rust
cargo +stable fmt --check
cargo +stable clippy --workspace --all-targets -- -D warnings
cargo +stable test --workspace
Set-Location ..
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm build
docker compose config
```

Dockerfile 构建检查可在 GitHub Actions 或具备 Docker 的受控环境完成。不得用 CI 连接 `futures` VPS、生产数据库或生产对象。

准入基线 CI run `30187416767` 与 Container images run `30187946869` 已在 SHA `636c8ae036f6ea65e8292bca19f38205db98f4a6` 成功。Generator 的每个候选提交仍必须推送开发分支并取得新的 CI success；准备 VPS 验收的候选 SHA 必须通过 container workflow 并记录 API、Worker、Frontend 的不可变 digest。

### 13.2 `futures` VPS 最终 Phase 3 验收

VPS 是迁移、真实 PostgreSQL、RLS、对象持久化和最终 E2E 环境。部署候选必须来自通过 GitHub Actions 的确定提交及 GHCR 不可变 digest；不得上传源码到 VPS 常规编译，也不得在 VPS 手工修改源码。

验收至少包括：

- 部署前数据库备份、生产 Compose 覆盖配置、三个 GHCR digest 的 `docker pull`、`docker compose up -d`、健康检查、真实 `GIT_SHA` 和迁移唯一性。
- 四种文件格式的上传、inspect、mapping、preview、validate、confirm、Worker、SSE、errors。
- 四冲突策略、幂等矩阵、并发确认、双 Worker、续租、SIGKILL 恢复、generation fence 和 dead-letter 回归。
- 无冲突整批回滚成功并验证逆序恢复。
- 后续修改、另一导入覆盖、下游依赖和并发竞争下整批零变更及完整冲突清单。
- 补偿批次完整流程及 lineage。
- 对象/数据库一致性 scan/check/quarantine；不触碰真实保留对象，并证明没有物理删除。
- 跨 Workspace API 与数据库 RLS 破坏性测试。
- Session 撤销后的 SSE 终止。
- API/PostgreSQL/Worker/Nginx 重启恢复。
- 日志、响应、审计、事件和证据秘密扫描。
- 测试用户、临时批次、临时对象、测试触发器和证据残留清理核验。

应输出版本化成功标记，例如 `PHASE3D_E2E_PASS`，并记录脚本 SHA-256、源码提交、镜像 digest、迁移列表和证据目录；不得在文档中记录秘密。

### 13.3 独立 Evaluator

- Generator 完成并提供本地、CI 和 VPS 证据后，必须调用独立 Evaluator。
- Evaluator 核验完整差异、迁移最终态、锁序、原子性、RLS、OpenAPI、前端、对象治理和秘密扫描。
- 所有 BLOCKER/HIGH 必须交回 Generator 修复并复核至 PASS。
- Phase 3 只有在 Evaluator 最终 `PASS` 且 `BLOCKER=0`、`HIGH=0` 时完成。

## 14. Generator 文件边界

获得用户明确授权后，Generator 仅可修改：

- `rust/migrations/`：Phase 3D 前向迁移，不改写已执行迁移。
- `rust/apps/api/`：回滚、补偿、lineage、对象检查相关路由/鉴权/OpenAPI。
- `rust/apps/worker/`：公平调度和受控对象治理 job；不得动态执行 payload。
- `rust/crates/database/`：事务、锁序、RLS 上下文、变更日志和回滚 repository。
- `rust/crates/domain/`：Phase 3D 受控类型、状态和错误。
- `rust/tests/`：API/DB/RLS/并发/对象一致性/VPS E2E。
- `frontend/src/`：仅导入中心完整流程、API 客户端和测试。
- `docs/API_DESIGN.md`、`docs/DATABASE_DESIGN.md`、`docs/IMPORT_DESIGN.md`、`docs/ENVIRONMENT.md`、`docs/DEPLOYMENT.md`、`docs/RELEASE_PROCESS.md`、`PLANS.md` 和阶段 handoff：仅同步实际实现与证据。
- 必要的导入专用测试夹具。

未经重新授权不得修改：

- `.github/workflows/`、`deploy/` 或 `docker-compose*.yml`，除非实现后发现 Phase 3D 验收的最小必要调整并先由用户确认。
- Phase 1/2 已 PASS 的身份、安全和 Workspace 行为。
- 行情、交易、持仓、套利、席位、采集、AI、回测模块。
- 既有已执行迁移。

## 15. 提交、合并与标签方案

1. 用户确认本文后，在 `phase/03-import-foundation` 实施，不创建新的业务阶段分支。
2. Generator 完成一个独立实现提交，建议提交信息：

   `feat: complete phase 3d import finalization`

3. Evaluator PASS、文档与 handoff 收口后创建独立收口提交，建议提交信息：

   `docs: close phase 3 import foundation`

4. 禁止 force push；开发分支先推送并等待新 CI/镜像工作流实际通过，准入基线 run 不替代 Phase 3D 候选门禁。
5. 只有本地门禁、GitHub Actions、`futures` VPS 最终 E2E 和独立 Evaluator 全部 PASS 后，才允许将 `phase/03-import-foundation` 合并到 `main`。
6. 使用保留 Phase 3A/3B/3C/3D 审计提交的普通 merge commit；禁止 squash、rebase 或改写已有阶段历史。
7. `main` 合并提交通过 CI 后，在该 `main` 提交创建带注释标签，建议：

   `phase-3-pass-YYYYMMDD`

   `YYYYMMDD` 必须使用实际完成日期；不得提前创建 PASS 标签。
8. Phase 3 收口不创建任何 `v*` 正式版本标签。
9. 推送标签后用 `git ls-remote --heads --tags origin` 核验分支和标签，再记录最终提交、CI run、镜像 digest、VPS 证据和 Evaluator 报告。

## 16. 退出条件

必须全部满足：

- 本文所列范围和关键决策已由用户明确确认，Generator 按任务包实施。
- 整批原子回滚、冲突零变更、补偿批次、lineage 和对象一致性治理已实现。
- 不存在部分回滚入口。
- Phase 3A 遗留 2 项和 Phase 3C 遗留 4 项 MEDIUM 均有关闭证据；Phase 3B 无重新打开的 MEDIUM。
- 本地和 GitHub CI 门禁通过。
- `futures` VPS 输出版本化 `PHASE3D_E2E_PASS`，全量 Phase 3 验收通过。
- 秘密扫描命中为 0。
- 独立 Evaluator 最终 PASS，`BLOCKER=0`、`HIGH=0`。
- Phase 3D 实现与收口分别提交，工作树干净，handoff 已更新。
- 独立 Evaluator PASS 后按普通 merge commit 合并 main，并在 main 合并提交创建实际日期 PASS 标签；不 squash/rebase，不创建 `v*`。

## 17. 已确认决策与阻塞处理

用户已经确认：

1. Phase 3D 按本文范围获得实施授权。
2. 排除 `cancel`、人工 dead-letter replay、冲突候选人工合并及第 3 节全部非目标。
3. API 同步全量预检并幂等入队；Worker 异步执行，正式逆变更、数据失效、审计、批次/任务状态和事件单事务原子提交；冲突整体中止。
4. 有后续修改或依赖时只允许可追溯补偿批次。
5. 对象治理只允许 scan、check、quarantine 和 audit，绝不物理删除。
6. 独立 Evaluator PASS 后普通 merge commit 合并 main，不 squash/rebase；创建实际日期 `phase-3-pass-YYYYMMDD`，不创建 `v*`。

若单事务回滚在 VPS 性能测试中超出安全上限，Generator 必须停止并报告 Planner，不能自行改为分块提交或部分回滚。除此之外，当前无产品决策阻塞。

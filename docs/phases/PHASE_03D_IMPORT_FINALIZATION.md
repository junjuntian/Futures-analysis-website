# Phase 3D：导入收口、原子回滚与补偿

## 1. 授权状态

**当前状态：待用户确认，未授权实现。**

本文仅是 Planner 交付的实施契约。当前不得调用 Generator，不得编写业务代码，不得创建或执行数据库迁移，也不得部署到 `futures` VPS。用户明确确认本文后，才可为 Phase 3D 单独授权 Generator。

准入基线：

- 开发分支为 `phase/03-import-foundation`。
- Phase 3A、3B、3C 均已完成并经独立 Evaluator PASS。
- Phase 3C 实现提交为 `04011ed`，收口提交为 `6e1d46d`。
- Phase 3D 必须在上述提交均为当前 HEAD 祖先、工作树干净、GitHub CI 基线可判定后开始。
- 开始实现前必须再次确认本文的阻塞问题；未确认项不得由 Generator 自行扩展。

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
- 浏览器识别、Playwright/noVNC 登录流程。
- OCR。
- AI。
- 自动回测。
- 任何行情、交易、套利等正式业务域表。
- 部分回滚、按行勾选回滚或“忽略冲突继续回滚”。
- 自动部署 `futures` VPS。

`cancel`、人工 dead-letter replay 和冲突候选人工合并不在本轮已明确需求中；除非用户在实施授权时明确加入，否则保持未实现。

## 4. 遗留 MEDIUM 归属

### 4.1 Phase 3A

| 项目 | 归属与 Phase 3D 处理 |
| --- | --- |
| multipart OpenAPI schema | 已在 Phase 3B 关闭；3D 仅做全量契约回归，不重复实现。 |
| 对象文件与数据库事务之间的孤儿治理 | 纳入 3D：持久化扫描/清理结果、故障注入、对象与数据库一致性检查。 |
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

以下是授权后 Generator 的模型契约；具体迁移版本号由 Generator 按仓库顺序生成，但在用户确认前不得创建迁移。

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

### 5.2 回滚请求、冲突和幂等

计划增加持久化的回滚请求/尝试和冲突明细模型，逻辑上至少保存：

- Workspace、原批次、发起人、幂等键摘要、状态和时间。
- 预检基线/指纹、冲突总数、完成结果。
- 冲突类型、目标 ID、期望版本、当前版本、依赖类型和脱敏说明。
- 与审计事件和批次事件的关联 ID。

不得保存明文 `Idempotency-Key`。冲突列表必须稳定排序并支持游标分页；重试同键同参返回原结果，同键异参返回稳定 `409`。

### 5.3 补偿批次

- `import_batches.compensates_batch_id` 指向同 Workspace 的已结束原批次。
- 一个补偿批次只能直接补偿一个批次；链路允许多层，但禁止自引用和循环。
- 创建补偿草稿不修改原批次或正式数据。
- 补偿必须经过完整 preview、validate、confirm 和 Worker 流程，不能绕过冲突策略或权限。
- 批次详情返回可遍历的直接前驱/后继摘要；链路查询必须有深度上限并检测循环。

### 5.4 来源与批次可追溯

任一 `imported_records` 记录必须能只读追溯到：

`imported_record → source_import_batch → import_row_change → source_file/source_row → stored_object(SHA-256) → mapping_version → confirmation/job → actor/audit`

回滚后仍保留原始文件、文件元数据、映射版本、staging/error 报告、变更日志、事件和审计。对象生命周期清理不得破坏法定/产品保留期内的追溯链。

## 6. 回滚 API 计划

授权后新增或完善：

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| `POST` | `/api/v1/imports/{import_id}/rollback-check` | 只读预检；返回预检指纹、可回滚性、完整冲突计数和首屏冲突。 |
| `GET` | `/api/v1/imports/{import_id}/rollback-conflicts` | 按稳定游标分页读取最近一次指定预检的冲突。 |
| `POST` | `/api/v1/imports/{import_id}/rollback` | 带预检指纹和 `Idempotency-Key` 执行整批原子回滚。 |
| `POST` | `/api/v1/imports/{import_id}/compensations` | 创建引用原批次的补偿草稿；后续复用现有 mapping/preview/confirm API。 |
| `GET` | `/api/v1/imports/{import_id}/lineage` | 返回批次、文件、对象、映射、确认、补偿与回滚链的脱敏摘要。 |

所有有副作用接口要求 Session、写权限、Origin、CSRF 和幂等校验。跨 Workspace 统一不可见；权限拒绝和业务拒绝均形成脱敏审计。

稳定错误至少包括：

- `rollback_not_allowed`
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

- 当前目标不存在、已软删除状态不符或 Workspace 不符。
- 当前 `row_version` 不等于导入完成时记录的版本。
- 当前值与 `after_json` 的受控比较不一致。
- 目标记录存在后续人工修改、其他导入批次覆盖或补偿。
- 存在受控依赖检查器报告的下游引用。
- 变更日志缺失、序号缺口、重复目标/非法操作或来源链断裂。

冲突检测器使用服务端受控枚举，不允许动态 SQL 或客户端声明依赖。

### 7.2 执行回滚

- 仅 `succeeded` 且未回滚的批次可执行。
- 执行事务必须重新锁定批次和全部受影响目标，再重复预检；客户端预检结果不能替代事务内检查。
- 固定锁序：Workspace/幂等 advisory lock → batch → rollback request → 按受控目标类型和目标 ID 排序的目标行。
- 全部通过后，按 `sequence_no DESC` 逆序恢复。
- `insert` 仅删除仍保持导入后版本且无依赖的记录。
- `update` 仅在版本匹配时恢复 `before_json`，并以新的版本表达回滚动作。
- `soft_delete` 恢复删除前状态。
- 任一检查或写入失败，批次状态、目标数据、回滚结果、事件和审计全部回滚；不允许已成功的子集落库。
- 成功时目标变更、批次 `rolled_back`、`rolled_back_at`、回滚请求终态、事件和审计同事务提交。
- 同批次并发回滚只允许一个执行；其余请求幂等重放或稳定冲突。

### 7.3 冲突零变更证明

自动化测试必须在事务前后对以下集合计算并比较摘要：

- 全部目标记录及版本。
- 原 `import_row_changes`。
- 批次终态、正式结果计数。
- 补偿链。

发现任一冲突时，除持久化的只读预检/冲突报告和审计外，上述业务集合必须逐字节等价；不得短暂提交后再补救。

## 8. 对象存储一致性治理

### 8.1 检查类别

- 数据库有 `stored_objects`，对象文件不存在。
- 对象存在但数据库无记录。
- 大小、SHA-256、backend、state 或 Workspace 路径不一致。
- `.tmp` 或 `pending` 对象超过阈值。
- 数据库提交不确定、删除失败或进程中断留下的待处理对象。
- 已被 `import_files` 引用的对象被错误标记可清理。

### 8.2 治理原则

- 扫描只允许在配置的对象存储根目录内运行，拒绝路径穿越和符号链接越界。
- 默认只报告；隔离/删除必须是独立、显式、可审计且幂等的运维动作。
- 有数据库引用、保留期未到、哈希未确认或状态不明的对象不得删除。
- 建议采用 `pending → available → quarantined/deleted` 的受控状态演进；真实删除失败保留可重试记录。
- 所有扫描结果按 Workspace 隔离；普通用户不得获知其他 Workspace 的对象键。
- 对象扫描和清理任务使用受控 job type，不允许 payload 注入路径。

### 8.3 自动化故障注入

必须覆盖 rename 后数据库失败、commit 响应不确定、删除失败、进程中断、对象缺失、哈希错误、孤儿对象、过期临时文件和重复清理。测试使用隔离临时对象根，不触碰 VPS 真实业务对象。

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

## 11. 自动化测试矩阵

### 11.1 Rust 单元与服务集成

- 变更日志序号、before/after、来源行和版本记录。
- insert/update/soft-delete 逆序算法。
- 所有状态转换和稳定错误映射。
- rollback/compensation 权限、CSRF、Origin 和幂等。
- OpenAPI rollback/compensation/SSE schema 契约。
- 对象一致性扫描的路径安全、分类和幂等。

### 11.2 PostgreSQL 与 RLS

- 新增表全部包含 `workspace_id`、索引、外键、强制 RLS。
- `futures_runtime` 保持非 superuser、无 `BYPASSRLS`。
- 跨 Workspace SELECT/INSERT/UPDATE/DELETE 全部拒绝。
- 变更日志不可变、序号唯一、补偿同 Workspace、禁止循环。
- 无冲突回滚成功；后续修改、后续导入覆盖、删除、依赖、版本不符、变更日志损坏均整批零变更。
- 两连接/多连接并发回滚、回滚与新修改竞争、回滚与补偿竞争无死锁且结果确定。

### 11.3 API

- rollback-check、conflicts 分页、rollback、compensation、lineage 正常/边界/恶意输入。
- 同键同参、同键异参和 20 路并发回滚。
- 跨 Workspace 统一不可见；viewer/未知角色拒绝。
- 预检后发生修改返回 `rollback_precondition_stale` 或完整冲突，业务零变更。
- 审计覆盖成功、失败、拒绝、幂等重放和冲突。

### 11.4 前端

- 上传到成功导入的主流程。
- inspect/mapping 变更后的状态失效。
- confirm、SSE 断线重连、errors 分页、终态。
- rollback-check、二次确认、冲突/陈旧预检、幂等重复点击。
- 补偿草稿与 lineage。
- Session/权限失效和 API 错误恢复。

### 11.5 对象一致性与安全

- 第 8.3 节全部故障注入。
- 扫描不得越出对象根目录，不跟随恶意符号链接。
- 日志、响应、事件、审计、错误报告和测试证据秘密扫描命中为 0。

### 11.6 全量回归

- Phase 1/2 认证、Session、Workspace、CSRF、审计和 RLS 回归。
- Phase 3A/3B/3C 的 TXT/CSV/XLS/XLSX、inspect、mapping、preview、四策略、Worker、SSE、幂等与并发回归。
- 现有 Phase 3B 5 项 MEDIUM 关闭证据继续通过。

## 12. 本地、CI、VPS 与 Evaluator 门禁

### 12.1 本地与 GitHub CI

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

### 12.2 `futures` VPS 最终 Phase 3 验收

VPS 是迁移、真实 PostgreSQL、RLS、对象持久化和最终 E2E 环境。部署候选必须来自本地 Git 的确定提交；不得在 VPS 手工修改源码。

验收至少包括：

- Compose 配置、镜像拉取/构建方式、健康检查、真实 `GIT_SHA` 和迁移唯一性。
- 四种文件格式的上传、inspect、mapping、preview、validate、confirm、Worker、SSE、errors。
- 四冲突策略、幂等矩阵、并发确认、双 Worker、续租、SIGKILL 恢复、generation fence 和 dead-letter 回归。
- 无冲突整批回滚成功并验证逆序恢复。
- 后续修改、另一导入覆盖、下游依赖和并发竞争下整批零变更及完整冲突清单。
- 补偿批次完整流程及 lineage。
- 对象/数据库一致性只读扫描；任何清理只针对专用测试夹具，不触碰真实保留对象。
- 跨 Workspace API 与数据库 RLS 破坏性测试。
- Session 撤销后的 SSE 终止。
- API/PostgreSQL/Worker/Nginx 重启恢复。
- 日志、响应、审计、事件和证据秘密扫描。
- 测试用户、临时批次、临时对象、测试触发器和证据残留清理核验。

应输出版本化成功标记，例如 `PHASE3D_E2E_PASS`，并记录脚本 SHA-256、源码提交、镜像 digest、迁移列表和证据目录；不得在文档中记录秘密。

### 12.3 独立 Evaluator

- Generator 完成并提供本地、CI 和 VPS 证据后，必须调用独立 Evaluator。
- Evaluator 核验完整差异、迁移最终态、锁序、原子性、RLS、OpenAPI、前端、对象治理和秘密扫描。
- 所有 BLOCKER/HIGH 必须交回 Generator 修复并复核至 PASS。
- Phase 3 只有在 Evaluator 最终 `PASS` 且 `BLOCKER=0`、`HIGH=0` 时完成。

## 13. Generator 文件边界

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
- 行情、交易、持仓、套利、席位、采集、浏览器、OCR、AI、回测模块。
- 既有已执行迁移。

## 14. 提交、合并与标签方案

1. 用户确认本文后，在 `phase/03-import-foundation` 实施，不创建新的业务阶段分支。
2. Generator 完成一个独立实现提交，建议提交信息：

   `feat: complete phase 3d import finalization`

3. Evaluator PASS、文档与 handoff 收口后创建独立收口提交，建议提交信息：

   `docs: close phase 3 import foundation`

4. 禁止 force push；开发分支先推送并等待 CI/镜像工作流实际通过。
5. 只有本地门禁、GitHub Actions、`futures` VPS 最终 E2E 和独立 Evaluator 全部 PASS 后，才允许将 `phase/03-import-foundation` 合并到 `main`。
6. 合并方式待用户确认；建议使用保留 Phase 3A/3B/3C/3D 审计提交的普通 merge commit，不 squash 已有阶段历史。
7. `main` 合并提交通过 CI 后，在该 `main` 提交创建带注释标签，建议：

   `phase-3-pass-20260726`

   若实际完成日期变化，标签日期应使用真实完成日；不得提前创建 PASS 标签。
8. 如同时发布正式版本，`v*` 版本号必须由用户另行确认；Phase PASS 标签不自动等同语义版本发布。
9. 推送标签后用 `git ls-remote --heads --tags origin` 核验分支和标签，再记录最终提交、CI run、镜像 digest、VPS 证据和 Evaluator 报告。

## 15. 退出条件

必须全部满足：

- 本文已由用户明确确认并授权实现。
- 整批原子回滚、冲突零变更、补偿批次、lineage 和对象一致性治理已实现。
- 不存在部分回滚入口。
- Phase 3A 遗留 2 项和 Phase 3C 遗留 4 项 MEDIUM 均有关闭证据；Phase 3B 无重新打开的 MEDIUM。
- 本地和 GitHub CI 门禁通过。
- `futures` VPS 输出版本化 `PHASE3D_E2E_PASS`，全量 Phase 3 验收通过。
- 秘密扫描命中为 0。
- 独立 Evaluator 最终 PASS，`BLOCKER=0`、`HIGH=0`。
- Phase 3D 实现与收口分别提交，工作树干净，handoff 已更新。
- 用户确认后才执行 main 合并与最终标签。

## 16. 阻塞与待用户确认

开始 Generator 前需用户确认：

1. 是否按本文授权 Phase 3D 实现。
2. `cancel`、人工 dead-letter replay、冲突候选人工合并是否继续排除；本文默认排除。
3. 回滚采用同步单事务执行，还是复用 `job_queue` 异步执行。建议先以数据正确性为门槛：回滚预检同步，执行走受控 Worker，但 Worker 内仍以单数据库事务保证整批原子性；若性能测试证明单事务超出 VPS 安全上限，应停止并重新规划，不能自行分块提交。
4. 对象治理的删除权限是否本阶段开放。建议本阶段默认“扫描 + 隔离 + 可审计重试”，真实物理删除仅限过期测试夹具或管理员显式动作。
5. Phase 3 完成后的合并方式是否采用普通 merge commit。
6. 最终 PASS 标签使用实际完成日期；是否还需要同时创建 `v*` 正式版本标签。

在以上问题确认前，Phase 3D 维持“待用户确认/未授权实现”。

# Phase 3：导入基础

计划日期：2026-07-25

当前状态：已拆分为 Phase 3A/3B/3C/3D；Phase 3A、Phase 3B、Phase 3C 已完成并经独立 Evaluator PASS；Phase 3D 范围与关键决策已由用户确认并获实施授权，详细契约见 `docs/phases/PHASE_03D_IMPORT_FINALIZATION.md`。

## 1. 背景与目标

Phase 1 已完成工程基础，Phase 2 已完成身份、个人 Workspace、Cookie Session、CSRF、权限基础、审计与 PostgreSQL RLS，并已在 `futures` VPS 通过部署验收和 Evaluator 复核。

Phase 3 的目标是建设 MVP 的第一个业务入口：导入中心基础能力。它只负责让用户把本地 TXT、CSV、XLS、XLSX 文件安全上传、识别、映射、预览、校验、确认、异步导入、查询进度、查看错误报告，并为后续行情、目录、交易、席位等正式业务数据接入提供可追溯、可审计、可回滚的批次框架。

本阶段不实现行情分析、套利统计、交易持仓、外部采集、OCR、AI 或自动回测。

## 2. 范围

### 2.1 文件上传与原始文件存储

- 支持上传 TXT、CSV、XLS、XLSX。
- 建立 `ObjectStorage` 抽象，第一版使用本地文件系统适配器。
- 原始文件按当前 `workspace_id` 隔离保存，生成不可猜测 `object_key`。
- 保存文件 SHA-256、大小、MIME、检测格式、原始文件名和保留状态。
- 上传限制必须覆盖文件大小、扩展名、magic bytes、MIME 不一致和危险路径名。
- 原始文件不得进入 Git、镜像、普通日志或未授权响应。

### 2.2 导入批次与状态机

- 建立 `import_batches`、`import_files`、`import_templates`、`import_template_versions`、`import_mappings`、`import_staging_rows`、`import_errors`、`import_row_changes` 等基础模型。
- 状态至少覆盖：
  - `uploaded`
  - `inspected`
  - `mapped`
  - `preview_ready`
  - `confirmed`
  - `importing`
  - `succeeded`
  - `failed`
  - `rollback_check`
  - `rolling_back`
  - `rolled_back`
  - `rollback_conflict`
  - `rollback_failed`
  - `expired`
- `confirmed` 后源文件、映射、冲突策略和确认参数不可变。
- 上传成功不等于正式导入成功。

### 2.3 编码、分隔符和工作表识别

- TXT/CSV 至少支持 UTF-8，并支持已批准的常见中文编码检测与人工覆盖。
- 分隔符支持逗号、制表符、分号、竖线、空格和用户指定值。
- Excel 支持工作表列表、表头行选择、前 50 行预览。
- Excel 不执行宏、外部链接、公式或嵌入对象；公式单元格必须作为风险或不可执行内容处理。
- 识别结果需要返回置信度、候选项和可人工覆盖字段。

### 2.4 字段映射模板

- 字段映射模板按 `workspace_id`、`dataset_type` 和版本管理。
- 模板版本不可变，批次引用固定 `mapping_version_id`。
- 映射目标字段必须来自服务端数据集定义，不允许用户自定义任意目标表或任意 SQL。
- 转换能力只使用受控枚举，例如日期格式、数字清洗、空值处理、枚举映射、单位换算占位。
- 预览中必须同时展示原始值、规范值、目标字段、错误和警告。

### 2.5 前 50 行预览

- 预览默认展示前 50 行数据。
- 预览响应包含列名、原始值、规范值、字段错误、行级错误、警告和候选映射。
- 预览同时触发全文件轻量扫描，产出总行数、可解析行数、错误数、警告数、重复数和冲突数。
- 预览不得写入正式业务表。

### 2.6 数据校验和错误报告

- 校验层级覆盖文件、结构、字段、跨字段、目录引用、业务唯一键和数据质量警告。
- 错误代码使用稳定英文枚举，用户消息使用中文且脱敏。
- 错误报告支持分页查询，至少包含 `row_number`、`field_name`、`error_code`、`raw_value`、`message`。
- 错误与警告不得泄漏其他 Workspace 的资源是否存在。

### 2.7 去重与冲突策略

- 文件内重复和数据库既有记录冲突必须分开统计。
- 冲突策略至少规划：
  - `skip`
  - `overwrite`
  - `abort`
- `keep_both` 仅在具体数据集模型支持时开放，本阶段默认不作为通用策略。
- 不同 `dataset_type` 必须定义自己的业务唯一键和允许策略。
- 同一文件、同一映射版本、同一确认参数和同一幂等键重试不得重复写入。

### 2.8 原子回滚与补偿批次

- 支持已成功批次的整批原子回滚。
- 回滚前必须检查全部 `import_row_changes`、目标行版本和下游引用。
- 任一后续修改或下游引用存在时，整个回滚零变更并返回完整冲突清单。
- 不提供部分回滚入口。
- 纠错使用新的补偿批次，并通过 `compensates_batch_id` 引用原批次。

### 2.9 Workspace 隔离、RLS 与审计

- 所有导入域业务表必须包含非空 `workspace_id`。
- 业务唯一约束和主要查询索引必须包含 `workspace_id`。
- 所有导入域业务表从迁移创建时启用并强制 PostgreSQL RLS。
- 应用层查询仍必须显式按当前 session 解析出的 Workspace 过滤，RLS 作为第二层防护。
- 导入上传、inspect、mapping、preview、confirm、rollback、补偿、错误查询和 SSE 事件均必须审计关键动作。
- 不允许客户端传入或切换 `workspace_id`。

### 2.10 SSE 导入进度

- 后台导入任务进度使用 SSE。
- 事件流按当前 `workspace_id` 和 session 权限过滤。
- 事件至少覆盖 queued、running、progress、waiting_for_user、succeeded、failed、dead_letter、rollback_conflict、rolled_back。
- 断线重连不得泄漏其他 Workspace 事件；重复事件不得造成重复写入。

### 2.11 本地测试与 futures VPS 验收

- 本地完成 Rust fmt/test/clippy、前端 lint/test/build、迁移检查和导入域单元/集成测试。
- GitHub Actions 负责权威 CI、Dockerfile 检查及 `linux/amd64` API、Worker、Frontend 镜像构建与 GHCR 发布。
- `futures` VPS 不承担常规 Rust/前端编译，也不接收源码包现场 build；只按不可变 digest pull 已验证镜像、备份数据库、执行迁移并完成真实 RLS、对象持久化和 E2E。
- VPS 验收必须覆盖上传、inspect、mapping、preview、confirm、SSE、错误报告、幂等、回滚、跨 Workspace 越权、RLS 和日志秘密扫描。

### 2.12 本轮 VPS 容量治理记录

- 清理前根分区：`/` 25G 总量、21G 已用、2.0G 可用、92% 使用率。
- 清理后根分区：`/` 25G 总量、9.1G 已用、14G 可用、40% 使用率。
- 清理范围仅限本项目 `/tmp` 临时部署包和 Docker build cache。
- 未删除 PostgreSQL 数据卷、对象存储卷或当前运行镜像。
- 本轮 Phase 3B 实施不得以容量治理为由改变业务语义；如需调整 multipart/body limit，只允许在契约要求内最小修改。

## 3. 非目标

Phase 3 明确排除以下能力：

- 套利统计、季节性、百分位、Z-Score 和图表。
- 交易、成交、持仓、绩效和账户管理。
- 外部网站采集、交易所连接器、授权网页访问和 noVNC。
- OCR 服务、OCR 结果确认和图片表格识别。
- AI 对话、AI 工具、向量检索和模型接入。
- 自动回测、自动交易信号、模拟成交和策略执行。
- 自动生成连续合约、换月规则计算和图表导出。

若实现导入需要测试目标表，本阶段只能使用导入域 staging/示例数据集或最小目录占位，不得提前实现行情、交易或套利正式业务逻辑。

## 4. 设计约束

- 文档与实现使用中文说明；代码标识符、API 字段、数据库字段使用英文 `snake_case`。
- 本地 Git 仓库是唯一源码源头，不得在 VPS 手工修改源码。
- 所有文档、日志、提交和回复不得包含密码、API Key、Cookie、Token、主密钥或数据库明文凭据。
- Cookie Session、CSRF、权限基础和 Workspace 解析沿用 Phase 2 实现，不做业务性重构。
- 所有上传和解析输入均视为不可信数据。
- Excel 宏、外部链接、公式、嵌入对象和异常压缩结构必须安全失败或进入警告，不得执行。
- 金融数值最终存储不得使用浮点数。
- 上传、解析、预览、正式提交和回滚必须可追溯到批次、文件、行号、映射版本和操作者。
- `confirm`、`rollback` 等有副作用接口必须校验 CSRF 和 `Idempotency-Key`。

## 5. 数据模型计划

### 5.1 存储对象

- `stored_objects`
  - `id`
  - `workspace_id`
  - `backend`
  - `object_key`
  - `sha256`
  - `size_bytes`
  - `mime_type`
  - `state`
  - `retention_until`
  - `created_by`
  - `created_at`

用途：记录原始文件对象元数据，不在数据库保存完整文件内容。

### 5.2 导入批次

- `import_batches`
  - `id`
  - `workspace_id`
  - `dataset_type`
  - `status`
  - `conflict_policy`
  - `mapping_version_id`
  - `compensates_batch_id`
  - `idempotency_key_hash`
  - `created_by`
  - `confirmed_at`
  - `committed_at`
  - `rolled_back_at`
  - `created_at`
  - `updated_at`

### 5.3 导入文件

- `import_files`
  - `id`
  - `workspace_id`
  - `batch_id`
  - `stored_object_id`
  - `original_name`
  - `sha256`
  - `detected_format`
  - `detected_encoding`
  - `detected_delimiter`
  - `selected_sheet`
  - `header_row`
  - `created_at`

### 5.4 模板与映射

- `import_templates`
- `import_template_versions`
- `import_mappings`

约束：

- 模板版本不可变。
- `configuration_json` 保存受控转换枚举和字段定义，不保存可执行脚本。
- `import_mappings` 绑定批次，不允许跨 Workspace 复用其他用户模板。

### 5.5 Staging、错误与变更日志

- `import_staging_rows`
- `import_errors`
- `import_row_changes`

约束：

- `import_staging_rows` 保留原始行和规范化行 JSON，用于预览、确认和调试。
- `import_errors` 记录错误和冲突，错误消息脱敏。
- `import_row_changes` 是回滚和补偿的核心依据，必须记录目标表、目标 ID、操作类型、变更前后 JSON、目标行版本和顺序号。

### 5.6 RLS 与索引

- 每张导入域表创建时 `enable row level security` 与 `force row level security`。
- 策略使用 `workspace_id = app.current_workspace_id()`。
- 主要唯一约束与查询索引均包含 `workspace_id`。
- 集成测试必须验证运行时角色无法跨 Workspace 读取、写入、回滚或订阅进度。

## 6. API 计划

所有接口使用 `/api/v1` 前缀，复用 Phase 2 Cookie Session 与 CSRF。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/imports` | 创建导入批次并上传文件 |
| `GET` | `/api/v1/imports/{import_id}` | 查询批次状态、计数、文件和参数摘要 |
| `POST` | `/api/v1/imports/{import_id}/inspect` | 识别格式、编码、分隔符、工作表、表头 |
| `PUT` | `/api/v1/imports/{import_id}/mapping` | 保存字段映射和转换参数 |
| `POST` | `/api/v1/imports/{import_id}/preview` | 生成前 50 行预览和错误摘要 |
| `POST` | `/api/v1/imports/{import_id}/confirm` | 冻结参数并提交后台导入任务 |
| `POST` | `/api/v1/imports/{import_id}/rollback-check` | 同步执行整批回滚全量预检 |
| `POST` | `/api/v1/imports/{import_id}/rollback` | 同步重验后幂等创建异步回滚任务 |
| `GET` | `/api/v1/imports/{import_id}/errors` | 分页读取错误、警告和冲突 |
| `GET` | `/api/v1/imports/{import_id}/events` | SSE 导入进度事件 |
| `GET` | `/api/v1/import-templates` | 查询当前 Workspace 可用映射模板 |
| `POST` | `/api/v1/import-templates` | 创建模板草稿或新版本 |

要求：

- `POST /imports` 使用 `multipart/form-data`。
- `confirm` 和 `rollback` 必须要求 `Idempotency-Key`。
- 所有副作用接口必须要求 CSRF。
- 对不可见或跨 Workspace 的 `import_id` 返回 404 或统一不可见错误，不泄漏存在性。
- OpenAPI 必须描述上传字段、错误体、CSRF header、幂等 header、状态枚举和 SSE 事件格式。

## 7. 前端计划

- 新增导入中心入口，不触碰 Phase 2 登录、Session 与 Workspace 基础页面的行为。
- 页面流程：
  1. 文件上传。
  2. 自动识别与人工修正编码、分隔符、工作表和表头。
  3. 选择数据集类型和字段映射模板。
  4. 查看前 50 行预览、错误、警告、重复和冲突统计。
  5. 选择允许的冲突策略并确认导入。
  6. 查看 SSE 进度、结果报告和错误分页。
  7. 对成功批次执行回滚检查和回滚确认。
- 前端不得允许用户输入 `workspace_id`。
- 前端测试覆盖主要状态切换、错误展示、CSRF 失败提示和 SSE 断线提示。

## 8. 分段实施计划与授权边界

四个子阶段必须依次独立完成 Generator 实施、本地测试、适用的 VPS 验证、Evaluator 审查、BLOCKER/HIGH 修复、Evaluator PASS 和 Git 提交。前一子阶段 PASS 不自动授权后一子阶段；未授权能力不得以“预留实现”“顺手补齐”或测试夹具名义提前落地。

### 8.1 Phase 3A：上传、文件存储抽象、文件哈希、批次状态机

**授权状态：当前唯一获准实施的子阶段。**

范围：

- 新增 `ObjectStorage` 接口和本地文件系统适配器；对象键必须不可猜测、不可由原始文件名构造，并限制在配置的存储根目录内。
- 以流式写入计算 SHA-256 和大小，使用临时文件加原子重命名完成持久化；失败时不得留下已登记但不可读的对象。
- 上传入口只接收 TXT、CSV、XLS、XLSX，校验大小、扩展名、基础 magic bytes/MIME 一致性和危险文件名；本阶段只验证可接收性，不解析表格内容。
- 仅新增 `stored_objects`、`import_batches`、`import_files` 三张表，包含 Workspace 约束、索引、外键、强制 RLS 和必要审计字段。
- 建立 `import_batches` 状态枚举及集中式合法转换规则；3A 只产生 `uploaded` 初始状态，并允许为测试验证状态机本身，不开放 inspect、confirm、导入任务或回滚业务入口。
- 只开放 `POST /api/v1/imports` 和 `GET /api/v1/imports/{import_id}`；复用 Phase 2 Session、CSRF、Workspace 解析、统一不可见错误和审计基础。
- OpenAPI 只描述上述两个接口、上传限制、响应模型和状态枚举。

Generator 精确文件范围：

- 可新增迁移：`rust/migrations/*_phase_3a_import_upload.sql`，不得在本阶段创建其他导入域表。
- 可新增并接线：`rust/crates/domain/src/import.rs`、`rust/crates/application/src/imports.rs`、`rust/crates/database/src/imports.rs`、`rust/crates/infrastructure/src/object_storage.rs`、`rust/apps/api/src/imports.rs`，以及这些 crate 现有 `lib.rs`/`main.rs` 中的最小模块声明和路由接线。
- 仅为上述代码所需时可修改相应 `Cargo.toml`、工作区 `rust/Cargo.toml` 与 `rust/Cargo.lock`；不得引入 CSV、Excel、任务队列或 SSE 依赖。
- 仅为配置本地对象存储根目录和容器持久卷所需时可修改 `.env.example`、`docker-compose.yml`；不得改变已有认证、数据库或反向代理语义。
- 测试必须与上述模块同目录，或放在既有 Rust 测试结构内；可以新增最小的 TXT/CSV/XLS/XLSX 上传夹具，但不得新增解析预期。
- 不得修改 `frontend/`、`rust/apps/worker/`、Phase 1/2 迁移、`docs/DECISIONS.md`，也不得创建映射、staging、错误、变更日志、任务或 SSE 表。

最大任务边界：

- 一次 HTTP 请求只完成“鉴权与 CSRF → 受限上传 → 对象持久化与哈希 → 三表元数据事务登记 → 返回 `uploaded` 批次”；不读取行列、编码、分隔符、工作表、表头或公式。
- 不实现 `/inspect`、`/mapping`、`/preview`、`/confirm`、`/cancel`、`/rollback`、`/errors`、`/events` 或模板接口。
- 不实现字段映射、数据校验、去重、冲突策略、幂等确认、PostgreSQL 任务队列、SSE、正式目标表写入、回滚、补偿批次或任何导入前端。
- 若现有架构阻止在上述范围内完成，Generator 必须报告阻塞，不得自行扩大边界。

验收标准：

- 四种允许扩展名的最小文件均可上传并形成同一 Workspace 下相互关联的三表记录；数据库和磁盘记录的 SHA-256、大小一致。
- 超限、空文件、扩展名/MIME 或 magic bytes 明显冲突、路径穿越文件名及中断写入被安全拒绝，且无越界文件、孤儿元数据或敏感日志。
- 对象键不包含原始文件名和 `workspace_id` 明文；同内容重复上传可以拥有独立批次，但哈希必须一致。
- 当前 Workspace 可查询自己的批次；跨 Workspace API 返回统一不可见错误，运行时数据库角色也无法跨 Workspace 读写三张表。
- 状态机单元测试覆盖全部允许转换和代表性非法转换；非法转换不得更新 `status` 或 `updated_at`。
- 服务/容器重启后已上传对象和元数据仍可查询；Phase 1/2 认证、健康和 RLS 基线不回归。

测试门禁：

- 本地：`git diff --check`、`cargo +stable fmt --check`、`cargo +stable test --workspace`、`cargo +stable clippy --workspace --all-targets -- -D warnings`；前端虽不得修改，仍运行 `pnpm lint`、`pnpm test`、`pnpm build` 作为回归门禁。
- `futures` VPS Docker：`docker compose --profile dev config --quiet`、build、`up -d --force-recreate`、容器健康、迁移记录、四类上传烟测、哈希与持久化核对、跨 Workspace API/RLS 破坏性测试、重启恢复和日志秘密扫描。
- Evaluator 必须实际独立审查 Phase 3A；所有 BLOCKER/HIGH 由 Generator 修复并复核至 PASS。

退出条件：

- 上述本地与 VPS 门禁证据齐全，Evaluator 最终 PASS 且无剩余 BLOCKER/HIGH。
- Phase 3A 单独 Git 提交完成，`PLANS.md` 更新为 3A 已完成、3B 待授权。
- 不以 3B/3C/3D 尚未实现作为 3A 失败理由；但发现任何越界实现必须在 3A 收口前移除。

### 8.2 Phase 3B：解析、识别、预览与字段映射

**授权状态：已完成并经独立 Evaluator PASS；Phase 3C/3D 仍未授权。**

范围：

- TXT/CSV/XLS/XLSX 安全解析。
- TXT/CSV 编码、分隔符自动识别，支持人工覆盖；分隔符候选包含逗号、制表符、分号、竖线、空格和用户指定值。
- Excel 支持工作表列表、工作表选择、表头行选择和前 50 行预览。
- Excel 宏、公式、外部链接、嵌入对象和异常压缩结构只识别并安全失败/警告，不执行。
- 字段映射、字段映射模板、模板不可变版本和批次映射。
- 生成前 50 行原始值、规范值、目标字段、字段错误、行级错误和警告预览；可做全文件轻量扫描元数据统计，但不得写入正式业务表。
- 解析错误展示和只读错误查询。
- 将 Phase 3A 遗留 OpenAPI multipart schema 问题纳入本轮 3B 契约修复，确保 `POST /api/v1/imports` 的 `multipart/form-data` 上传字段、文件字段 schema、错误体和前端客户端契约一致。

禁止范围：

- 不做正式确认入库。
- 不实现冲突策略落库、冲突策略写入或正式业务目标表写入。
- 不实现 SSE、PostgreSQL 任务队列、后台 job semantics、取消任务语义、回滚或补偿批次。
- 不实现套利、交易、AI、OCR、外部采集、行情分析、自动回测或图表能力。

Generator 文件范围：

- `docs/` 可更新。
- Rust 仅限 `domain`、`application`、`database`、`infrastructure`、`api` 中 import 相关模块，以及必要的 `Cargo.toml`、`Cargo.lock`。
- 可新增 Phase 3B migration，用于模板、模板版本、映射、staging preview 和 errors metadata。
- `frontend/` 仅允许导入中心 3B 最小页面、API 客户端和测试；不得实现 confirm、SSE、rollback 或完整 3D 流程。
- `deploy/nginx` 仅在 multipart/body limit 契约需要时可最小调整，不得语义扩张。

API 边界：

- 保留 Phase 3A 已有 `POST /api/v1/imports` 与 `GET /api/v1/imports/{import_id}`。
- 可按需新增 `inspect`、`mapping`、`preview`、`import-templates` 和只读 `errors` 接口。
- 不得新增 `/confirm`、`/events`、`/rollback`、`/cancel`，也不得引入 job semantics。
- OpenAPI 必须与实现一致，尤其是 multipart 上传 schema、CSRF/error 契约、3B 新增接口请求/响应和状态枚举。

DB 边界：

- 允许新增 `import_templates`、`import_template_versions`、`import_mappings`、staging preview 和 errors metadata。
- 新增表必须包含 Workspace 隔离、RLS、索引、审计字段和跨 Workspace 测试。
- 不得新增 `import_row_changes`、jobs、正式业务目标表写入、冲突策略写入或正式导入结果表。

frontend 边界：

- 只允许最小导入中心页面展示上传后 inspect、mapping、preview、模板和错误展示能力。
- 不得暴露确认导入、任务进度、SSE、取消、回滚、补偿或正式业务数据入口。
- 前端不得允许用户输入或切换 `workspace_id`。

验收标准：

- TXT、CSV、XLS、XLSX 均有正常、边界和恶意样例覆盖。
- 编码与分隔符自动识别和人工覆盖有效。
- Excel 工作表选择、表头行选择、公式/宏/外链/嵌入对象安全处理有效。
- 前 50 行预览严格限流，展示原始值、规范值、目标字段、错误和警告。
- 字段映射模板版本不可变，旧批次引用旧版本。
- 解析错误可展示、可分页只读查询，且错误消息脱敏。
- OpenAPI multipart schema 修复通过契约检查，前端客户端与后端实现一致。
- 跨 Workspace API/RLS 隔离通过；不得出现 3C/3D 越界接口、表或 UI。

测试门禁：

- 本地必须通过 `git diff --check`、`cargo +stable fmt --check`、`cargo +stable test --workspace`、`cargo +stable clippy --workspace --all-targets -- -D warnings`。
- 前端若有变更，必须通过 `pnpm lint`、`pnpm test`、`pnpm build`。
- `futures` VPS 执行适用于 3B 的 Docker config/build/up、迁移记录、OpenAPI 契约、四类文件 inspect/mapping/preview、错误展示、跨 Workspace API/RLS 和日志秘密扫描。
- Evaluator 必须独立审查 Phase 3B；所有 BLOCKER/HIGH 由 Generator 修复并复核至 PASS。

退出条件：3B 功能和证据单独提交，`PLANS.md` 更新为 3B 已完成、3C 待授权，且仓库不存在 3C/3D 越界实现。

收口结果（2026-07-25）：

- 实现提交：`150194c feat: complete phase 3b import parsing preview mapping`。
- 本地 Rust fmt/test/clippy、前端 lint/test/build 和 `git diff --check` 通过。
- `futures` VPS 已完成容量治理、迁移 `202607250003` 至 `202607250007`、API 镜像重建、健康检查、Phase 3B E2E、数据库不变量及并发绑定测试。
- 独立顶层 Evaluator 最终结论为 PASS，`BLOCKER=0`、`HIGH=0`；非阻断 MEDIUM 记录于 `docs/reviews/PHASE_03B_EVALUATION.md`。
- 未发现 confirm/events/rollback/cancel、任务队列、SSE、正式确认入库、冲突策略或 `import_row_changes` 等 3C/3D 越界实现。

### 8.3 Phase 3C：校验、去重、冲突、确认任务与 SSE

**授权状态：已完成并经独立 Evaluator PASS；本节收口时 Phase 3D 尚未授权，当前 Phase 3D 已按第 8.4 节另行授权。**

#### 8.3.1 本轮目标、状态与硬边界

Phase 3C 只交付以下闭环：

1. 对 Phase 3B staging 数据执行确定性的字段、跨字段、业务唯一键和受控引用校验。
2. 分开统计文件内重复与正式导入域已有记录冲突。
3. 实现 `skip`、`overwrite`、`keep_conflict`、`abort` 四种冲突策略。
4. 通过 `/confirm` 冻结参数并创建 PostgreSQL `job_queue` 任务，由 Worker 正式写入导入域通用目标表。
5. 对确认重试、并发确认、Worker 重试和租约恢复提供幂等保证。
6. 通过持久化事件和 `Last-Event-ID` 提供可重连、可重放的 SSE 进度。
7. 对新增数据、API、任务、事件和审计实施 Workspace 隔离、强制 RLS 与最小权限控制。
8. 只补充确认、冲突策略选择和进度展示所需的最小前端。

本轮不创建行情、交易、持仓、套利或其他业务域目标表。正式写入的唯一目标是导入域通用表 `imported_records`，用于验证正式导入、唯一性、冲突和并发语义；其存在不代表任何行情或交易数据模型已获授权。

#### 8.3.2 校验、业务唯一性与冲突语义

- 业务唯一键由服务端按 `dataset_type` 定义并规范化，客户端不得提交 SQL、列名、表达式或自定义唯一键。Phase 3C 只允许已具备确定性唯一键定义的数据集确认；其他数据集返回稳定错误 `dataset_not_confirmable`。
- 校验结果必须绑定 `import_batch_id`、`mapping_version_id`、staging 版本和校验版本。inspect、mapping 或 preview 变化后，旧校验结果立即失效，必须重新校验。
- blocking error、warning、文件内 duplicate 和数据库 conflict 分别计数；错误代码保持稳定英文枚举，中文消息脱敏。
- 文件内重复按规范化业务唯一键分组，并按原始 `row_number` 确定顺序：
  - `skip`：仅第一条有效候选可进入正式写入，其余标记为 skipped。
  - `overwrite`：仅最后一条有效候选可进入正式写入，同一批次同一业务键最多产生一条正式记录。
  - `keep_conflict`：重复组候选不写入 `imported_records`，全部保存在 `import_conflict_candidates`，等待未来另行授权的处理流程。
  - `abort`：存在任一文件内重复即禁止确认；若确认后因并发才出现冲突，则 Worker 整次正式写入事务失败且不产生部分正式记录。
- 与 `imported_records` 已有业务键冲突时：
  - `skip`：保留既有正式记录，候选只计入 skipped。
  - `overwrite`：更新同一正式记录的受控 `record_data`、来源批次、来源行号和行版本。
  - `keep_conflict`：既有正式记录保持不变，候选完整保存在 `import_conflict_candidates`。
  - `abort`：该批次不产生任何正式写入，并形成稳定冲突错误。
- `keep_conflict` 明确不是 `keep_both`：它不会绕过业务唯一约束，不会在 `imported_records` 中保存第二条同键记录，也不提供本轮内的人工解决入口。
- blocking validation error 与非法策略均在确认前拒绝；Worker 仍必须重新检查冻结版本和数据库唯一约束，以处理校验后至正式写入前的竞争。

#### 8.3.3 正式目标与数据库迁移

Phase 3C 只允许新增两组迁移，文件名匹配：

- `rust/migrations/*_phase_3c_validation_and_imported_records.sql`
- `rust/migrations/*_phase_3c_job_queue_and_events.sql`

第一组迁移可扩展 `import_batches`、`import_staging_rows`、`import_errors` 的 3C 字段，并仅新增：

- `imported_records`
  - `id`
  - `workspace_id`
  - `dataset_type`
  - `business_key`
  - `record_data`
  - `source_import_batch_id`
  - `source_row_number`
  - `row_version`
  - `created_by`
  - `created_at`
  - `updated_at`
  - 唯一约束：`(workspace_id, dataset_type, business_key)`
- `import_conflict_candidates`
  - `id`
  - `workspace_id`
  - `import_batch_id`
  - `staging_row_id`
  - `dataset_type`
  - `business_key`
  - `candidate_data`
  - `existing_record_id`
  - `conflict_kind`
  - `created_at`
  - 唯一约束必须防止同一 staging 行因任务重试重复保存。

第二组迁移仅新增：

- `import_confirmations`
  - 保存 `workspace_id`、`import_batch_id`、`idempotency_key_hash`、规范化请求 `request_hash`、确认响应引用、操作者和时间。
  - `(workspace_id, idempotency_key_hash)` 唯一；只保存带服务端 pepper 的摘要，不保存原始 `Idempotency-Key`。
- `job_queue`
  - 保存 `workspace_id`、`job_type`、`aggregate_id`、`status`、`payload`、`attempt_count`、`max_attempts`、`available_at`、`leased_by`、`lease_expires_at`、`last_error_code` 和时间戳。
  - `job_type=import_confirm` 时 `(workspace_id, job_type, aggregate_id)` 唯一，保证一个批次最多一个正式导入任务。
- `import_job_events`
  - 保存 `workspace_id`、`import_batch_id`、`job_id`、批次内单调递增 `event_seq`、`event_type`、脱敏 `payload` 和 `created_at`。
  - `(workspace_id, import_batch_id, event_seq)` 唯一，SSE 的 `id` 即十进制 `event_seq`。

所有新增表从创建时即满足：

- `workspace_id NOT NULL`，外键不得跨 Workspace；主要索引以 `workspace_id` 为首列。
- `ENABLE ROW LEVEL SECURITY` 与 `FORCE ROW LEVEL SECURITY`，策略使用 `workspace_id = app.current_workspace_id()`。
- 应用查询仍显式带当前 Workspace 条件；客户端请求和 SSE 参数均不得接收 `workspace_id`。
- 运行时 API/Worker 角色只拥有所需表和操作的最小权限。
- 不新增 `import_row_changes`，不新增任何 market/trading/arbitrage 表，也不为 Phase 3D 预建回滚或补偿结构。

#### 8.3.4 API 与 OpenAPI 契约

Phase 3C 允许新增或补齐的接口只有：

| 方法 | 路径 | 契约 |
| --- | --- | --- |
| `POST` | `/api/v1/imports/{import_id}/validate` | 对当前 mapping/staging 版本执行校验并返回错误、警告、duplicate、conflict 计数；要求 Session、权限和 CSRF |
| `GET` | `/api/v1/imports/{import_id}/errors` | 将 Phase 3B 固定上限改为稳定游标分页；游标绑定 Workspace、批次与排序键 |
| `POST` | `/api/v1/imports/{import_id}/confirm` | 请求体只含允许的 `conflict_policy`；要求 Session、权限、CSRF 与 `Idempotency-Key`；首次成功返回 `202`，幂等重放返回相同批次/任务摘要 |
| `GET` | `/api/v1/imports/{import_id}/events` | `text/event-stream`；读取 `Last-Event-ID` header，按当前 Workspace 和批次重放后继续实时推送 |
| `GET` | `/api/v1/imports/{import_id}` | 补充校验摘要、冻结策略、任务状态、进度和终态摘要，不返回原始幂等键或敏感行内容 |

统一错误至少覆盖 `validation_required`、`validation_stale`、`blocking_errors_present`、`conflict_policy_not_allowed`、`idempotency_key_reused`、`confirmation_conflict`、`event_id_invalid` 和 `event_not_visible`。OpenAPI 必须同步请求/响应、header、状态码、游标、冲突策略枚举和 SSE 事件 schema。

本轮明确不新增 `/cancel`、`/rollback`、补偿、冲突人工解决或 dead-letter replay API。

#### 8.3.5 确认并发、幂等与事务边界

`POST /confirm` 必须在一个数据库事务内完成以下步骤：

1. 设置当前 Workspace RLS 上下文并 `SELECT ... FOR UPDATE` 锁定批次。
2. 验证批次可确认、校验版本未失效、无 blocking error、策略被该数据集允许。
3. 对批次、mapping/staging/validation 版本、策略和操作者作用域生成规范化 `request_hash`。
4. 查询或写入 `import_confirmations`：
   - 同一幂等键且同一 `request_hash`：返回最初响应，不创建第二个任务。
   - 同一幂等键但不同 `request_hash`：返回 `409 idempotency_key_reused`。
5. 若批次已由另一幂等键确认：
   - 冻结指纹相同：关联并返回同一 `job_id`，不重复冻结或入队。
   - 冻结指纹不同：返回 `409 confirmation_conflict`。
6. 首次确认时冻结 mapping、校验版本、冲突策略和确认参数，将批次转为 `confirmed`，插入唯一 `job_queue` 行、首个 `queued` 事件和确认审计。
7. 一次提交上述全部记录；任一步失败均不留下“已确认但未入队”或“已入队但未确认”的状态。

要求至少用同键重试、同键异参、不同键同参、不同键异参和多连接并发确认覆盖该事务。数据库普通事务失败回退是实现原子提交的基础，不等同于 Phase 3D 的用户可调用批次回滚能力。

#### 8.3.6 PostgreSQL `job_queue`、Worker、租约、重试与死信

- Worker 以 `FOR UPDATE SKIP LOCKED` 从 `status=queued` 且 `available_at <= now()`，或租约已过期的 `running` 任务中领取一条任务。
- 领取时原子设置 `status=running`、`leased_by`、`lease_expires_at` 并递增 `attempt_count`；默认租约 30 秒，执行期间每 10 秒续租。
- Worker 只处理 `job_type=import_confirm`；未知类型作为永久错误，不得动态执行 payload 中的代码、SQL、表名或列名。
- 正式写入时锁定批次并验证冻结指纹。对业务键按稳定顺序处理，使用数据库唯一约束及受控 INSERT/UPDATE 实现策略。
- 一次 Worker 尝试把 `imported_records`/`import_conflict_candidates` 写入、批次终态、任务终态、最终计数和终态事件放在同一事务中。进程在提交前退出不会留下部分正式数据；提交后退出也不会因租约重领再次产生正式效果。
- 进度事件可按确定性分段提交，但不得改变正式写入幂等结果；重复领取不得重复事件终态或降低已报告进度。
- 仅瞬时数据库连接错误、明确列入 allowlist 的可重试 SQLSTATE 和临时基础设施错误可重试。校验、权限、未知数据集、非法策略和确定性业务冲突是永久错误。
- 默认 `max_attempts=5`；重试延迟为 2、4、8、16 秒并封顶 60 秒，写入 `available_at`。测试可通过配置缩短时间，但不得改变次数语义。
- 超过最大尝试后将任务置为 `dead_letter`，批次置为 `failed`，持久化 `dead_letter` SSE 事件和脱敏审计。
- Worker 重启后自动领取租约过期任务。Phase 3C 不提供取消、不提供人工 dead-letter 重放；人工 replay 和运维控制面必须等待后续单独授权。

#### 8.3.7 SSE 持久化、隔离与 `Last-Event-ID` 重放

- 事件类型仅包括 `queued`、`running`、`progress`、`succeeded`、`failed`、`dead_letter`；本轮没有 `cancelled`。
- API 在建立流前完成 Session、批次可见性和 Workspace 权限校验；不可见批次使用统一不可见错误，不先返回 SSE 200。
- 首次连接从该批次最早保留事件开始；重连时只读取 `Last-Event-ID` header，并重放 `event_seq > Last-Event-ID` 的持久化事件，再订阅新事件。
- 非十进制、负数或大于当前批次最大序号的 `Last-Event-ID` 返回稳定 400；不得将其他批次或 Workspace 的 event id 当作游标。
- 每 15 秒可发送不含业务数据的 heartbeat comment；建议客户端重连间隔为 3 秒。终态事件发送完毕后服务端可关闭连接。
- Phase 3C 不删除 `import_job_events`，从而保证本阶段验收窗口内无重放缺口；事件清理、归档和手工恢复不在本轮实现。
- SSE payload 只包含状态、已处理/总数、四类结果计数和稳定错误码，不含 Cookie、Token、原始行、完整 `record_data` 或其他 Workspace 信息。

#### 8.3.8 审计

至少记录：

- 校验完成/失败及摘要计数。
- 确认接受、幂等重放、幂等键冲突和并发确认冲突。
- Worker 成功、最终失败和 dead-letter。
- `skip`、`overwrite`、`keep_conflict`、`abort` 的聚合结果。
- 权限拒绝沿用现有统一审计策略；SSE 不逐条审计 heartbeat 或 progress。

审计只保存幂等键摘要前缀或不可逆关联 ID，不保存原始键、原始行、完整候选 JSON、Session/Cookie/Token 或数据库凭据。

#### 8.3.9 最小前端

前端只在现有 Phase 3B 导入页面补充：

- “校验”动作和 blocking error、warning、文件内 duplicate、数据库 conflict 四类摘要。
- 服务端返回的允许策略选择；`keep_conflict` 文案必须明确“保留候选、正式表不新增同键记录”。
- “确认导入”动作；为同一批次生成并在当前页面会话内稳定复用一个 `Idempotency-Key`，网络重试不得生成新键。
- queued/running/progress/succeeded/failed/dead_letter 进度展示。
- SSE 自动重连并携带最后已处理 `event_seq`；连接失败时用批次 GET 状态作为只读兜底。
- errors 游标分页。

不增加取消、回滚、补偿、dead-letter 手工重放、冲突人工解决、完整导入中心改版或任何行情/交易/套利图表。

#### 8.3.10 Phase 3B 五项 MEDIUM 的处理分类

Phase 3B 评审实际有五项 MEDIUM，本轮全部显式排期，不按“三项”摘要遗漏：

| # | 发现 | Phase 3C 分类与处理 |
| --- | --- | --- |
| 1 | 同参数 inspect 时前端仍清空预览 errors | 3C 前端前置修复；仅当 `preview_invalidated=true` 才清空，增加状态回归测试 |
| 2 | errors API 固定 `LIMIT 500` | 3C 正式范围；改为 Workspace/批次绑定的稳定游标分页 |
| 3 | 映射写入失败后的 staging/errors/status 事务一致性未直接测试 | 3C 数据库回归测试；这里验证普通数据库事务原子性，不实现批次回滚 API |
| 4 | 模板 `dataset_type` 冻结竞争未覆盖 | 3C 并发数据库回归测试；两连接竞争后不变量保持且无锁序反转 |
| 5 | 两份数据库脚本迁移前置注释止于 006 | 3C 测试维护；改为实际依赖 007，并由测试启动时检查迁移前置 |

#### 8.3.11 Generator 精确文件范围

仅允许修改或新增以下范围：

- 迁移：上述两个 `rust/migrations/*_phase_3c_*.sql` 文件。
- Domain/Application：
  - `rust/crates/domain/src/import.rs`
  - `rust/crates/domain/src/lib.rs`
  - `rust/crates/application/src/imports.rs`
  - `rust/crates/application/src/import_jobs.rs`
  - `rust/crates/application/src/lib.rs`
- Database：
  - `rust/crates/database/src/imports.rs`
  - `rust/crates/database/src/job_queue.rs`
  - `rust/crates/database/src/lib.rs`
- API：
  - `rust/apps/api/src/imports.rs`
  - `rust/apps/api/src/main.rs`
  - 现有 OpenAPI 定义文件中仅 import schema/path 段。
- Worker：
  - `rust/apps/worker/src/main.rs`
  - `rust/apps/worker/src/import_jobs.rs`
- 依赖接线：仅上述 crate 的 `Cargo.toml`、`rust/Cargo.toml`、`rust/Cargo.lock`；优先复用现有依赖，不得引入通用脚本执行、消息代理或外部队列。
- 前端：
  - `frontend/src/views/ImportCenterView.vue`
  - `frontend/src/api/imports.ts`
  - `frontend/src/types/imports.ts`
  - `frontend/src/router/index.ts` 仅在现有导入入口接线确有需要时最小修改
  - 与上述 import 页面/API 同名或同目录的测试和 Phase 3C fixtures。
- 数据库与 E2E 测试：现有 import 测试文件，以及新增文件名包含 `phase_3c`、`import_job`、`import_confirm` 或 `import_sse` 的测试/脚本。
- 文档：`PLANS.md`、本文件、import/API/database 设计中与 3C 契约直接相关的段落，以及 3C review/handoff。

若仓库当前实际文件名与上述计划名不同，Generator 必须先在实施记录中给出一一映射，并保持同一模块边界；不得借重命名扩大到认证、Workspace、行情、交易或其他页面。`docker-compose.yml` 只允许为现有 Worker 传入租约/重试配置作最小修改；Nginx 不需要为 SSE 之外的语义改动。

#### 8.3.12 明确延期与禁止项

以下项目明确延期，Phase 3C 不得实现，也不得以“预留表/路由/UI/测试夹具”名义创建：

- cancel API、取消状态转换、取消安全点或取消 SSE 事件。
- 手工 dead-letter replay、运维重放按钮或任务管理控制面。
- 原子批次回滚、部分回滚、回滚检查、补偿批次。
- `import_row_changes` 及其任何替代表、触发器或影子日志。
- 冲突候选人工解决/提升为正式记录。
- 完整前端导入中心改版。
- 行情、交易、持仓、套利、图表、外部采集、OCR、AI、回测或自动交易。

#### 8.3.13 本地测试门禁

必须通过：

```powershell
git diff --check
Set-Location rust
cargo +stable fmt --check
cargo +stable test --workspace
cargo +stable clippy --workspace --all-targets -- -D warnings
Set-Location ..
pnpm lint
pnpm test
pnpm build
```

自动化测试至少覆盖：

- 字段/跨字段/唯一键/受控引用校验的成功、blocking error 和 warning。
- 四策略乘以文件内 duplicate、既有 DB conflict、校验后并发插入冲突的矩阵。
- `keep_conflict` 只写 `import_conflict_candidates` 且绝不形成第二条正式同键记录。
- `imported_records` 唯一约束和跨 Workspace 同业务键可共存。
- 同键同参重放、同键异参冲突、不同键同参并发收敛到同一任务、不同键异参冲突；至少 20 个并发确认只产生一个 job 和一份正式效果。
- Worker 双实例 `SKIP LOCKED`、租约续期、进程退出后的过期租约恢复、瞬时失败重试、永久失败不重试、第五次失败进入 dead-letter。
- 任务提交前进程失败无部分正式数据，提交后模拟重复领取无重复写入。
- SSE 初连、断线、`Last-Event-ID` 精确重放、终态关闭、非法 event id、跨批次和跨 Workspace 拒绝。
- 所有新增表的 API 隔离与运行时角色强制 RLS 破坏性测试。
- 审计存在且秘密/原始数据扫描为零命中。
- 第 8.3.10 节五项 MEDIUM 各有对应测试或可核查脚本差异。
- 仓库不存在 `/cancel`、`/rollback`、dead-letter replay、`import_row_changes` 或正式业务域表新增。

#### 8.3.14 `futures` VPS 与 Evaluator 门禁

VPS 必须完成：

1. 从本地 Git 工作树构建部署包，校验 SHA-256；VPS 不手工改源码。
2. `docker compose --profile dev config --quiet`、build、`up -d --force-recreate` 和服务健康检查。
3. 执行并核验两组 Phase 3C 迁移、RLS policy、索引、唯一约束和运行时角色权限。
4. 使用导入域通用数据集执行 validate → confirm → Worker → SSE → `imported_records` E2E，分别验证四种策略。
5. 对同批次执行并发 confirm，核验一个 job、一个终态、无重复正式记录。
6. Worker 处理中重启容器，等待租约过期后恢复；再注入可重试失败与永久失败，核验 retry/dead_letter。
7. 中断 SSE 后携带 `Last-Event-ID` 重连，核验无丢失、无跨 Workspace 事件。
8. 以两个 Workspace 执行 API、数据库、任务、正式记录、冲突候选、事件和审计的越权/强制 RLS 测试。
9. 核验 Phase 1/2 认证、CSRF、健康接口和 Phase 3A/3B 上传/inspect/mapping/preview 回归。
10. 扫描日志、响应、错误、SSE 和审计，秘密及原始敏感行命中数必须为 0。
11. 核验没有 cancel/rollback/replay 路由，没有 `import_row_changes`，没有行情、交易、套利或其他正式业务表。

独立 Evaluator 必须：

- 审阅完整差异、迁移最终态、OpenAPI、队列状态机、确认事务、Worker 租约、SSE 重放、RLS/审计和最小前端。
- 逐项核验五个 Phase 3B MEDIUM 的处理证据。
- 复核本地与 VPS 证据，不以只读审查替代真实 Docker/数据库/E2E。
- 将所有 BLOCKER/HIGH 交回 Generator 修复，并复核至最终 PASS；3C 未 PASS 前不得授权 3D。

退出条件（已满足）：3C 功能、测试和 VPS/Evaluator 证据单独提交，`PLANS.md` 更新为 3C 已完成且 Evaluator PASS；3C 收口时仓库不存在任何 3D 或其他业务域越界实现。Phase 3D 当前授权见第 8.4 节。

收口结果（2026-07-26）：

- 实现提交：`04011ed feat: complete phase 3c validation and async import`。
- 本地 Rust fmt、47 项 workspace tests、clippy `-D warnings`、前端 lint、5 项测试、生产 build 和 `git diff --check` 通过。
- `futures` VPS 已部署最终 API/Worker 镜像，迁移 `202607250008`、`202607250009` 已执行；最终版本化 E2E 输出 `PHASE3C_E2E_PASS`，证据目录为 `/tmp/phase3c-e2e-559842`。
- E2E 覆盖 75 行正式导入、20 路并发确认、幂等四组合及跨批次竞争、4 策略 × 3 冲突来源矩阵、双 Worker、续租、SIGKILL 恢复、generation fence、瞬时重试、永久失败、第五次 dead-letter、SSE 重放/拒绝、5/5 RLS 读写、审计和秘密扫描。
- 独立 Evaluator 最终结论为 PASS，`BLOCKER=0`、`HIGH=0`；报告见 `docs/reviews/PHASE_03C_EVALUATION.md`。
- 4 项非阻断 MEDIUM 明确延期：SSE OpenAPI 帧 schema、跨 Workspace 公平轮询、长连接周期性重验会话、确认面板与断线重连组件级测试。
- 未实现 cancel、dead-letter replay、回滚、补偿批次、`import_row_changes`、行情、交易、套利、图表、OCR、AI 或外部网站采集。

### 8.4 Phase 3D：原子回滚、补偿批次、完整前端与部署验收

**授权状态：用户已确认范围与关键决策，Phase 3D 已获实施授权；Generator 按 `docs/phases/PHASE_03D_IMPORT_FINALIZATION.md` 的小任务包实施。**

固定语义：

- API 同步执行全量预检；`POST /rollback` 必须再次同步重验，通过后才幂等创建唯一 `import_rollback` job 并返回 `202`。
- Worker 异步执行并在事务内再次全量预检。成功时全部逆变更、数据失效记录、批次/rollback request/job 终态、事件和审计单事务提交；冲突时正式目标零变更，冲突清单、`rollback_conflict`、任务终态、事件和审计单事务提交。
- 任一后续修改或下游依赖使整批直接回滚失败；不提供部分回滚或绕过入口，只允许创建通过 `compensates_batch_id` 追溯且走完整导入流程的补偿批次。
- Phase 3C 已成功批次没有完整 change log，禁止伪造 backfill。只有带 `rollback_capability`、`change_log_version` 或等价完整能力标记的新批次可直接回滚；旧批次只允许补偿。
- 对象治理只允许 scan、consistency check、quarantine 和 audit；Phase 3D 绝不物理删除对象。
- 明确排除 cancel、人工 dead-letter replay、冲突候选人工合并、套利/交易/持仓/席位/图表、外部采集、浏览器识别、OCR、AI 和自动回测。

云端准入：SHA `636c8ae036f6ea65e8292bca19f38205db98f4a6` 的 CI run `30187416767` 和 Container images run `30187946869` 已成功；它们只证明实施前基线，Phase 3D 候选提交必须重新取得 CI 与镜像成功结果。

验收与退出：按 GHCR 不可变 digest 部署候选到 `futures` VPS，完成备份、迁移、真实 RLS、对象持久化、全量 E2E 和秘密扫描；独立 Evaluator 最终 PASS 且无 BLOCKER/HIGH 后收口。

## 9. Phase 3 总体验收标准

以下是 Phase 3D 最终收口时的 Phase 3 总体验收标准：

- TXT、CSV、XLS、XLSX 至少各有正常、边界和恶意样例。
- UTF-8 与批准的中文编码可正确预览；检测失败时可人工选择。
- CSV/TXT 分隔符自动识别和人工覆盖有效。
- XLS/XLSX 工作表、表头行和前 50 行预览有效；宏、公式、外部链接和异常结构不被执行。
- 上传文件保存为 Workspace 隔离对象，SHA-256 和元数据可追溯。
- `import_batches` 状态机符合设计，非法状态转换被拒绝。
- 映射模板版本不可变，旧批次引用旧版本。
- 预览显示原始值、规范值、目标字段、错误和警告。
- 文件内重复和数据库冲突分别统计。
- `skip`、`overwrite`、`abort` 在允许的数据集上符合定义；不允许的数据集策略被拒绝。
- 同一文件、同一映射、同一确认参数和同一幂等键重试不会重复写入。
- 无后续修改和依赖时，成功批次可整批原子回滚。
- 任一后续修改或下游依赖存在时，回滚整批零变更并返回完整冲突。
- 不存在部分回滚入口。
- 补偿批次可追溯到原批次。
- 导入批次、文件、staging、错误、变更日志、任务和 SSE 事件均受 Workspace 隔离。
- PostgreSQL RLS 跨 Workspace 读写破坏性测试通过。
- 上传、确认、回滚、补偿和权限拒绝均有审计记录。
- 日志、响应、错误报告和审计中不出现 Cookie、Token、密码、数据库凭据或原始文件敏感片段。
- OpenAPI 与实现一致。
- 本地测试和 `futures` VPS 验收全部通过。
- Evaluator 最终结论为 PASS，且无剩余 BLOCKER/HIGH。

## 10. 测试命令

本地建议命令：

```powershell
git diff --check
Set-Location rust
cargo +stable fmt --check
cargo +stable test --workspace
cargo +stable clippy --workspace --all-targets -- -D warnings
Set-Location ..
pnpm lint
pnpm test
pnpm build
```

VPS 验收命令类别：

```bash
docker compose --profile dev config --quiet
docker pull <api-digest>
docker pull <worker-digest>
docker pull <frontend-digest>
docker compose --profile dev up -d
docker compose --profile dev ps
```

数据库与 API 验收类别：

- 迁移记录检查。
- RLS policy 和运行时角色检查。
- 上传、inspect、mapping、preview、confirm、events、errors、rollback 的 HTTP E2E。
- 跨 Workspace 读取、写入、回滚和 SSE 越权测试。
- 服务重启恢复测试。
- 日志秘密扫描。

## 11. 本地与 futures VPS 验收流程

本节是 Phase 3D 的最终全流程。

1. 将 Phase 3D 候选提交推送到 GitHub 私有仓库，等待该 SHA 的 CI 成功。
2. 通过 container workflow 构建并发布 `linux/amd64` API、Worker、Frontend 镜像，记录 SHA 标签和三个完整 digest。
3. 部署前在 `futures` VPS 备份 PostgreSQL，并记录上一稳定版本三个镜像 digest；数据库备份与主密钥恢复副本分离。
4. 只传递生产 Compose 覆盖配置和非秘密发布元数据；不得上传源码到 VPS 常规编译，不得在 VPS 手工修改源码。
5. 在 VPS 按完整 digest 执行 `docker pull`，校验 Compose config 后执行 `docker compose up -d`。
6. 执行数据库迁移，并核验 `schema_versions`、RLS、索引、约束与运行时最小权限。
7. 运行 API live、ready、version、真实 `GIT_SHA` 和 OpenAPI 检查。
8. 执行导入 E2E：
   - 上传四类文件。
   - inspect 参数识别。
   - mapping 保存。
   - 前 50 行 preview。
   - confirm 后 SSE 进度。
   - errors 查询。
   - 幂等重试。
   - 成功回滚。
   - 回滚冲突零变更。
   - 旧 Phase 3C 批次不可直接回滚且只允许补偿。
   - 补偿批次与 lineage。
   - 对象 scan/check/quarantine，物理删除数为 0。
9. 执行跨 Workspace 越权和 RLS 破坏性测试。
10. 执行服务重启、Worker 租约恢复和 SSE 撤权终止测试。
11. 执行日志、响应、事件、审计和证据秘密扫描。
12. 调用独立 Evaluator 审查，修复 BLOCKER/HIGH 后复核至 PASS。
13. Evaluator PASS 后以普通 merge commit 合并 `main`，禁止 squash/rebase；创建实际日期 `phase-3-pass-YYYYMMDD`，不创建 `v*`。

## 12. 风险与开放问题

- 文件大小、最大行数、最大列数、最大工作表数、解析超时和 staging 保留周期仍需在实现前按 VPS 容量给出初始上限。
- XLS 支持可能引入额外解析依赖，需要 Generator 在实现前评估 Rust 生态库能力、许可证和安全边界。
- Excel 公式单元格的显示值来源需要明确：只读取缓存值或标记需人工处理，不执行公式。
- `overwrite` 策略必须绑定具体数据集；如果 Phase 3 不实现正式行情/交易目标表，需要使用导入域示例数据集验证机制，不得提前扩大业务域。
- 大批次是否单事务提交或 staging 后可恢复提交，需要基于性能测试决定；无决定前优先保证正确性和可回滚性。
- SSE 事件保留和重放窗口需要设置初始值。
- 本地对象存储路径、容量清理和备份策略需要与 VPS 磁盘容量匹配。

## 13. Generator 边界

Phase 3A、3B、3C Generator 工作均已完成并收口。Phase 3D 已获得独立授权，Generator 只能按第 8.4 节及 `docs/phases/PHASE_03D_IMPORT_FINALIZATION.md` 的任务包实施，不得沿用或扩大历史授权。

获得实施授权后，Generator 仍必须遵守：

- 不修改 Phase 1、Phase 2 已 PASS 的行为，除非导入基础接入所必需且有测试证明不破坏原行为。
- 不实现套利统计、图表、交易持仓、外部采集、OCR、AI 或自动回测。
- 不在 VPS 手工修改源码。
- 不把秘密写入 Git、数据库、镜像、日志、测试输出或回复。
- 任何新增业务表必须包含 `workspace_id`、RLS、索引、审计和跨 Workspace 测试。
- 所有新接口必须更新 OpenAPI、前端客户端和测试夹具。
- BLOCKER/HIGH 必须由 Generator 修复，并由 Evaluator 复核至 PASS 后才能提交。
- Generator 若再次卡住，允许创建新的 Generator 接手同一个 Phase 3C；新任务必须逐项引用第 8.3 节的文件范围、API/DB/frontend 边界、验收标准和最大边界，且不得由主 Agent 代替实现或评估。

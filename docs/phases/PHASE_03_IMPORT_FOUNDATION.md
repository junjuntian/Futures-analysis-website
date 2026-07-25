# Phase 3：导入基础

计划日期：2026-07-25

当前状态：计划已制定，等待项目所有者确认；尚未开始实现。

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
  - `cancelled`
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
- 导入上传、inspect、mapping、preview、confirm、cancel、rollback、错误查询和 SSE 事件均必须审计关键动作。
- 不允许客户端传入或切换 `workspace_id`。

### 2.10 SSE 导入进度

- 后台导入任务进度使用 SSE。
- 事件流按当前 `workspace_id` 和 session 权限过滤。
- 事件至少覆盖 queued、running、progress、waiting_for_user、succeeded、failed、cancelled、dead_letter。
- 断线重连不得泄漏其他 Workspace 事件；重复事件不得造成重复写入。

### 2.11 本地测试与 futures VPS 验收

- 本地完成 Rust fmt/test/clippy、前端 lint/test/build、迁移检查和导入域单元/集成测试。
- 本机没有 Docker 不作为阻塞；Docker Compose config、镜像构建、容器启动和部署测试在 `futures` VPS 执行。
- VPS 验收必须覆盖上传、inspect、mapping、preview、confirm、SSE、错误报告、幂等、回滚、跨 Workspace 越权、RLS 和日志秘密扫描。

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
| `POST` | `/api/v1/imports/{import_id}/cancel` | 取消尚未提交或安全检查点可停止的任务 |
| `POST` | `/api/v1/imports/{import_id}/rollback` | 整批原子回滚成功批次 |
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

## 8. 任务拆分

### P3-01 计划与契约冻结

- 确认 Phase 3 范围、非目标和验收口径。
- 更新 Phase 3 计划、PLANS、API/OpenAPI 契约草案。
- 明确样例文件目录和证据格式。

### P3-02 数据库迁移与 RLS

- 新增存储对象和导入域表。
- 添加状态枚举、唯一约束、外键、索引和 RLS policy。
- 编写跨 Workspace RLS 测试。

### P3-03 ObjectStorage 本地适配器

- 实现本地对象存储接口、对象键生成、SHA-256 校验和文件限制。
- 验证路径穿越、重复哈希和删除/保留策略。

### P3-04 文件识别与 inspect

- 实现 TXT/CSV 编码与分隔符检测。
- 实现 XLS/XLSX 工作表、表头和危险内容识别。
- 输出候选项、置信度和可人工覆盖参数。

### P3-05 映射模板与预览

- 实现字段映射模板版本化。
- 实现前 50 行预览、规范化、错误和警告展示。
- 实现全文件轻量扫描统计。

### P3-06 后台导入任务与 SSE

- 复用或扩展 Phase 1/2 Worker 基础。
- 实现导入任务状态、事件、进度、取消安全点和 SSE 查询。
- 保证任务查询与事件流按 Workspace 隔离。

### P3-07 校验、冲突和幂等

- 实现文件内重复、数据库冲突和允许策略。
- 实现 `skip`、`overwrite`、`abort` 的受控语义。
- 实现 `Idempotency-Key` 重试不重复写入。

### P3-08 回滚与补偿批次

- 实现整批回滚检查、冲突清单、逆序回滚和 `rolled_back` 状态。
- 实现补偿批次元数据引用。
- 明确不提供部分回滚入口。

### P3-09 前端导入中心

- 实现上传、inspect、mapping、preview、confirm、进度、错误报告、回滚页面。
- 加入前端状态测试和失败路径提示。

### P3-10 本地与 VPS 验收

- 本地跑完整 Rust/前端验证。
- 通过源码包传输部署到 `futures` VPS。
- 在 VPS 执行 compose config/build/up、迁移、健康、导入 E2E、RLS、SSE、回滚、日志秘密扫描。
- 调用 Evaluator 独立审查，修复 BLOCKER/HIGH 后复核至 PASS。

## 9. 验收标准

Phase 3 只有同时满足以下条件才能标记完成：

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
- 上传、确认、取消、回滚和权限拒绝均有审计记录。
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
docker compose --profile dev build
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

1. 本地只从 Git 工作树打包源码，排除 `.git`、`node_modules`、`target`、`dist`、`.env`、`secrets`、上传文件和临时文件。
2. 通过已配置 SSH/SFTP 上传到 `futures` VPS 的 `/tmp`，校验本地与远端 SHA-256。
3. 部署前备份 `/opt/futures-platform` 当前版本。
4. 解压覆盖到 `/opt/futures-platform`；禁止直接在 VPS 修改源码。
5. 在 VPS 执行 Compose config、build、up。
6. 执行数据库迁移，并核验 `schema_versions`。
7. 运行 API live、ready、version 和 OpenAPI 检查。
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
9. 执行跨 Workspace 越权和 RLS 破坏性测试。
10. 执行服务重启恢复测试。
11. 执行日志秘密扫描。
12. 调用 Evaluator 独立审查，修复 BLOCKER/HIGH 后复核至 PASS。

## 12. 风险与开放问题

- 文件大小、最大行数、最大列数、最大工作表数、解析超时和 staging 保留周期仍需在实现前按 VPS 容量给出初始上限。
- XLS 支持可能引入额外解析依赖，需要 Generator 在实现前评估 Rust 生态库能力、许可证和安全边界。
- Excel 公式单元格的显示值来源需要明确：只读取缓存值或标记需人工处理，不执行公式。
- `overwrite` 策略必须绑定具体数据集；如果 Phase 3 不实现正式行情/交易目标表，需要使用导入域示例数据集验证机制，不得提前扩大业务域。
- 大批次是否单事务提交或 staging 后可恢复提交，需要基于性能测试决定；无决定前优先保证正确性和可回滚性。
- SSE 事件保留和重放窗口需要设置初始值。
- 本地对象存储路径、容量清理和备份策略需要与 VPS 磁盘容量匹配。

## 13. Generator 边界

Generator 在收到明确实施授权前不得开始编码。

获得实施授权后，Generator 仍必须遵守：

- 不修改 Phase 1、Phase 2 已 PASS 的行为，除非导入基础接入所必需且有测试证明不破坏原行为。
- 不实现套利统计、图表、交易持仓、外部采集、OCR、AI 或自动回测。
- 不在 VPS 手工修改源码。
- 不把秘密写入 Git、数据库、镜像、日志、测试输出或回复。
- 任何新增业务表必须包含 `workspace_id`、RLS、索引、审计和跨 Workspace 测试。
- 所有新接口必须更新 OpenAPI、前端客户端和测试夹具。
- BLOCKER/HIGH 必须由 Generator 修复，并由 Evaluator 复核至 PASS 后才能提交。

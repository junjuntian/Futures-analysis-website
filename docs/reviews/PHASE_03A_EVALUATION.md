# Phase 3A 独立评审报告

初审日期：2026-07-25
复审日期：2026-07-25
评审范围：仅 Phase 3A（上传、对象存储抽象、文件哈希、`import_batches` 状态机）
最终结论：**PASS**

## 1. 范围与基线

- 当前分支：`phase/03-import-foundation`。
- 基线 HEAD：`cde16ee docs: plan phase three import foundation`。
- 复审重新检查了完整工作区差异、Phase 3A 计划、`docs/DECISIONS.md`、两次 Phase 3A 迁移、对象存储、上传/API/OpenAPI、Session/CSRF/权限/Workspace 复用、RLS、审计、测试和 VPS 黑盒证据。
- 未发现 TXT/CSV/XLS/XLSX 内容解析、编码/分隔符/工作表识别、预览、字段映射、校验去重、任务队列、SSE、回滚、补偿批次或导入前端等 Phase 3B/3C/3D 越界实现。
- `frontend/`、Worker、Phase 1/2 迁移和 `docs/DECISIONS.md` 均未被 Phase 3A 业务实现修改。

## 2. BLOCKER / HIGH

复审后无剩余 BLOCKER 或 HIGH。

### HIGH-01：上传缺少写权限检查

状态：**已关闭**。

证据：

- `rust/apps/api/src/auth.rs` 新增集中式 `Permission::ImportRead`、`Permission::ImportUpload` 与角色矩阵。
- `admin`、`analyst` 具有读取和上传权限；`viewer` 只有读取权限；未知/空角色没有导入权限。
- `rust/apps/api/src/imports.rs` 复用统一 `AuthContext`，上传前调用 `require_permission(Permission::ImportUpload)`，查询前调用 `ImportRead`，已删除 import 模块内复制的 Session/CSRF/Cookie/token 摘要 SQL。
- VPS 黑盒复验：`analyst` 上传成功；`viewer` 上传返回 `403 permission_denied`，对象和三表均零新增；`viewer` 可读取自己 Workspace 的批次，跨 Workspace GET 返回统一 `404`。
- 单元测试覆盖 admin/analyst/viewer 与未知角色矩阵。

### HIGH-02：CSRF/权限拒绝没有审计

状态：**已关闭**。

证据：

- 上传入口在鉴权后生成并贯穿同一个 `request_id`。
- Origin、CSRF、上传权限拒绝统一调用 `record_upload_denied`，写入 `import.upload`、`outcome=denied` 和脱敏 `error_code`；响应与审计使用同一 request ID。
- 审计写入失败时不静默放行，而是返回 `500`，保证拒绝事件不会在无审计的情况下继续。
- VPS 黑盒复验：
  - viewer 上传：`403 permission_denied`，审计为 `denied/permission_denied`；
  - 缺 CSRF：`403 csrf_required`，审计为 `denied/csrf_required`；
  - 错误 Origin：`403 origin_mismatch`，审计为 `denied/origin_mismatch`；
  - 响应 request ID 与审计 request ID 一致。
- 审计元数据没有 Cookie、CSRF、文件内容或其他秘密。

### HIGH-03：Nginx 25 MiB 与应用 50 MiB 上限不一致

状态：**已关闭**。

证据：

- 应用 `IMPORT_MAX_BYTES` 仍为 50 MiB；Axum multipart body limit 为文件上限加 1 MiB 固定开销。
- `deploy/nginx/nginx.conf` 的 `client_max_body_size` 已改为 `51m`，与上述部署边界一致。
- VPS 黑盒复验：合法 30 MiB CSV 经 Nginx 返回 `201`；数据库与磁盘大小均为 `31457280`，SHA-256 一致。
- 超过 50 MiB 的文件返回 `413`，没有新增对象、批次或文件元数据。

### HIGH-04：`stored_objects` 缺少对象后端、状态和保留字段

状态：**已关闭**。

证据：

- 前向修正迁移 `202607250002_phase_3a_stored_object_lifecycle.sql`：
  - 将 `content_type` 重命名为 `mime_type`；
  - 新增非空 `backend`、非空 `state` 和可空 `retention_until`；
  - 为 backend/state 添加受控检查约束；
  - 添加按 Workspace/保留期的条件索引。
- 上传登记显式写入 `backend=local`、`state=available`、`retention_until=NULL`。
- VPS 已执行且只执行一次 `202607250002`；最终 schema versions 为：
  - `202607240001`
  - `202607240002`
  - `202607250001`
  - `202607250002`
- VPS 黑盒复验确认 `content_type` 已移除；5 个既有对象和新测 30 MiB 对象均能得到正确生命周期字段。

## 3. MEDIUM

以下项目维持初审的 MEDIUM 定级，均为明确的非阻断后续项；未发现证据要求升级为 BLOCKER/HIGH。

### MEDIUM-01：OpenAPI multipart schema 没有结构化描述 `file` 字段

状态：**未关闭，非阻断**。

证据：

- `rust/apps/api/src/imports.rs` 仍以 `content = String` 声明 `multipart/form-data`。
- 复审直接读取 VPS `/api-docs/openapi.json`，`POST /api/v1/imports` request body 为：
  - content type：`multipart/form-data`
  - schema：`{"type":"string"}`
- 运行时接口和状态码已验证，但客户端生成器无法从契约识别必填 `file` 二进制字段。

建议：

- 定义专用 Utoipa multipart schema，明确必填 `file`、`type=string`、`format=binary`，并加入 OpenAPI 契约测试。

### MEDIUM-02：对象文件与数据库事务之间仍缺少可恢复的孤儿治理

状态：**未关闭，非阻断**。

证据：

- 对象仍先原子 rename 为正式文件，再登记数据库。
- 明确数据库失败时会尝试删除对象，但删除错误仍被忽略；数据库 commit 响应不确定且随后的查询也失败时，没有 durable cleanup 记录或孤儿重扫机制。
- VPS 正常/拒绝/超限路径验证为 `disk_objects=5`、三表 `5/5/5`、`.tmp=0`，证明当前部署没有实际孤儿；但故障注入窗口仍未形成可重复保障。

建议：

- 后续使用 `pending -> available` 对象状态、可重试清理任务或可审计孤儿扫描，补充数据库 commit 不确定、删除失败和进程中断测试。

### MEDIUM-03：缺少可重复的 API/数据库/RLS 自动集成测试

状态：**未关闭，非阻断**。

证据：

- 新增自动测试覆盖上传校验、状态机、本地对象存储和导入角色矩阵。
- 上传 multipart 路由、三表事务、拒绝审计、RLS 跨 Workspace 和非法状态转换不改 `updated_at` 主要依赖 VPS 黑盒/破坏性验证，尚未沉淀为 SQLx/Axum 自动集成测试。
- VPS 证据充分证明当前候选版本通过 Phase 3A 门禁，但未来回归保护仍不足。

建议：

- 增加 Axum router 与 SQLx 测试数据库集成测试，覆盖 multipart、CSRF、角色拒绝审计、404 隐身、RLS 跨读写和状态转换原子性。

## 4. LOW / SUGGESTION

### LOW-01：OpenAPI 响应 envelope 仍沿用既有简化描述

实际响应使用 `ApiResponse<T>` 的 `data/meta.request_id` envelope，而 path 注解直接声明内部 response/error 类型。这是既有 API 契约风格问题，本阶段没有新增更严重的运行时影响；建议与 MEDIUM-01 一并在契约测试中统一。

### SUGGESTION-01：为 Phase 3B 预留受控对象读取端口

当前 `ObjectStorage` 只定义上传和删除。进入 Phase 3B 前建议补充受控 read/open 与完整性验证接口，避免解析器绕过抽象直接读取本地路径；本建议不要求在 Phase 3A 越界实现解析。

## 5. 已验证通过项

### 本地门禁

- `git diff --check`：PASS。
- `cargo +stable fmt --check`：PASS。
- `cargo +stable test --workspace --offline`：PASS，16/16。
- `cargo +stable clippy --workspace --all-targets -- -D warnings`：PASS。
- `pnpm lint`：PASS。
- `pnpm test`：PASS，1/1。
- `pnpm build`：PASS。

### futures VPS

- 增量源码包本地/远端 SHA-256 一致。
- `docker compose --profile dev config --quiet`：PASS。
- API 镜像构建：PASS，exit 0。
- 五个服务运行；API/PostgreSQL healthy。
- live、ready、version、OpenAPI：HTTP 200。
- 迁移 `202607250002` 成功且 schema version 唯一。
- 三张导入域表均启用并强制 RLS；运行时角色保持 `NOSUPERUSER`、`NOBYPASSRLS`。
- 权限矩阵、合法 30 MiB、超限、CSRF、Origin、跨 Workspace 404、审计 request ID、磁盘/数据库哈希与大小全部通过。
- 最终清理后：测试用户与 Session 为 0；业务对象/批次/文件为 `5/5/5`；真实对象卷 `disk_objects=5`、`.tmp=0`；五个对象均为 `local/available/NULL`。
- API/Nginx 日志测试秘密扫描命中为 0。

## 6. Phase 3A 边界结论

- 仅创建 `stored_objects`、`import_batches`、`import_files` 三张导入域表及同一 Phase 3A 的生命周期前向修正迁移。
- 只开放 `POST /api/v1/imports` 与 `GET /api/v1/imports/{import_id}`。
- 没有解析文件内容，没有编码/分隔符/工作表/表头识别。
- 没有 mapping、preview、confirm、cancel、rollback、errors、events 或模板接口。
- 没有 staging、error、row change、job queue、SSE、正式目标表、回滚或补偿实现。
- 没有导入前端。

结论：**无 Phase 3B、3C、3D 越界实现。**

## 7. 最终状态

**PASS**

Phase 3A 无剩余 BLOCKER/HIGH。上述 MEDIUM/LOW/SUGGESTION 已明确记录为非阻断后续项，不得在本轮以修复它们为由扩大到 Phase 3B/3C/3D。

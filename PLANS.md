# 项目计划状态

## 当前阶段

阶段 3：导入基础；Phase 3A、Phase 3B 已完成并经独立 Evaluator PASS，Phase 3C/3D 继续未授权。

状态：Phase 1、Phase 2 均已完成并经 Evaluator PASS。Phase 3 已拆为 3A、3B、3C、3D。Phase 3A 以 `1b089f7 feat: complete phase 3a import upload foundation` 收口。Phase 3B 已完成实现、本地测试、`futures` VPS Docker/E2E 验证和独立顶层 Evaluator 复核，以 `150194c feat: complete phase 3b import parsing preview mapping` 提交；最终 `BLOCKER=0`、`HIGH=0`。Phase 3C、3D 均未授权且未实现，进入下一子阶段前必须重新授权。

## 本阶段任务状态

| 任务 | 状态 | 验证方式 |
| --- | --- | --- |
| 原始方案归档 | 已完成 | 源文件与 `docs/reference/` 副本 SHA-256 一致 |
| 方案结构化读取 | 已完成 | 198 段、36 张表、71 个标题、2 张图片；无批注和修订 |
| 冲突、缺失与风险审阅 | 已完成 | 已写入架构、安全、导入和待确认文档 |
| 产品边界与模块拆分 | 已完成 | 产品、架构、模块文档互相引用且范围一致 |
| 数据库与 API 逻辑设计 | 已完成 | 表域、关键字段、约束、接口和错误语义已定义 |
| 分阶段计划与验收 | 已完成 | 每阶段均有交付物、准入条件、退出条件和证据要求 |
| 首批决策同步 | 已完成 | `docs/DECISIONS.md` 与产品、架构、数据、安全、导入、API、计划和验收口径一致 |
| 剩余技术决策同步 | 已完成 | `DEC-026` 至 `DEC-037` 已新增，相关开放事项已关闭 |
| 三 Agent 角色配置 | 已完成 | `.agents/Planner.md`、`.agents/Generator.md`、`.agents/Evaluator.md` |
| 上下文交接机制 | 已完成 | `docs/handoffs/README.md` 与 `docs/handoffs/LATEST.md` |
| Phase 0 文档审查 | 已完成 | `docs/reviews/DOC_REVIEW_PHASE_0.md` |
| Phase 1 计划 | 已完成 | `docs/phases/PHASE_01_FOUNDATION.md` |
| Phase 1 工程实现 | 已完成 | 已创建 Git、Rust/Vue/Docker/Nginx/CI 基础文件；Rust 与前端验证通过；远端 Compose 配置通过 |
| futures VPS 部署验证 | 已完成 | 已通过 MCP SSH 安装 Docker/Compose、部署容器、执行迁移并完成健康检查 |
| Phase 1 Evaluator 审查 | 已完成 | `docs/reviews/PHASE_01_EVALUATION.md`，最终状态 PASS |
| Phase 2 接管核验 | 已完成 | 以 Git、迁移记录和 `futures` VPS 实态重新核验，不盲目信任交接摘要 |
| Phase 2 Planner 计划 | 已确认 | `docs/phases/PHASE_02_IDENTITY_WORKSPACE_SECURITY.md`；最终安全参数已固化 |
| Phase 2 工程实现 | 已完成 | 后端身份 API、Cookie Session、CSRF、权限基础、审计、RLS 迁移、前端身份页面和部署配置已实现 |
| Phase 2 本地验证 | 已完成 | Rust fmt/test/clippy 通过；前端 lint/test/build 通过 |
| Phase 2 VPS 部署验证 | 已完成 | 容器重建并启动；健康、迁移、RLS、认证 E2E、日志秘密扫描和重启恢复均通过 |
| Phase 2 Evaluator 审查 | 已完成 | `docs/reviews/PHASE_02_EVALUATION.md`，最终状态 PASS |
| Phase 2 版本收口 | 已完成 | `main`、`phase/01-foundation` 和标签 `phase-2-pass-20260725` 均指向 Phase 2 PASS 提交 `6dfea78` |
| Phase 3 分支创建 | 已完成 | 当前分支为 `phase/03-import-foundation`，从 `main` 创建 |
| Phase 3 Planner 拆分 | 已完成 | `docs/phases/PHASE_03_IMPORT_FOUNDATION.md` 已明确 3A/3B/3C/3D 的边界、门禁和退出条件 |
| Phase 3A：上传与批次基础 | 已完成、已提交、Evaluator PASS | 上传、文件存储抽象、文件哈希、`import_batches` 状态机已实现；提交 `1b089f7`，Evaluator `BLOCKER=0`、`HIGH=0` |
| Phase 3B：解析、预览与映射 | 已完成、已提交、Evaluator PASS | TXT/CSV/XLS/XLSX 解析、编码/分隔符识别与人工覆盖、Excel 工作表/表头选择、前 50 行预览、字段映射与版本模板、错误展示、OpenAPI multipart schema 契约修复；提交 `150194c`，Evaluator `BLOCKER=0`、`HIGH=0` |
| Phase 3C：校验与异步确认 | 未授权、未实现 | 3B PASS 并提交后再单独授权；本轮禁止实现 |
| Phase 3D：回滚与完整流程 | 未授权、未实现 | 3C PASS 并提交后再单独授权；本轮禁止实现 |

## Phase 3A 收口核验

核验日期：2026-07-25。

| 项目 | 实际结果 |
| --- | --- |
| Git 分支 | `phase/03-import-foundation` |
| Git 工作区 | Phase 3A 提交后干净；本轮仅更新 `PLANS.md` |
| Phase 3A 实现提交 | `1b089f7 feat: complete phase 3a import upload foundation` |
| 实现基线 | Phase 3A 实现收口为 `1b089f7`；Phase 2 收口为 `6dfea78` |
| Phase 1 结论 | Evaluator 最终 PASS |
| Phase 2 结论 | Evaluator 最终 PASS |
| Phase 3A 结论 | Evaluator 最终 PASS；`BLOCKER=0`、`HIGH=0` |
| VPS 容器 | PostgreSQL、API、Worker、Frontend、Nginx 均运行 |
| VPS 健康 | API/PostgreSQL healthy；live/ready/version/首页/Nginx 代理 HTTP 200 |
| 当前部署版本 | `0.1.0`；`git_sha=local` |
| Phase 3A 数据库迁移 | `202607250001`、`202607250002` 已执行 |
| Phase 3A RLS | 上传域 RLS 与跨 Workspace 拒绝验证通过 |
| 源码一致性 | VPS 部署目录来自源码包 overlay，远端不保留 `.git`，以本地 Git 为唯一源码源头 |

收口结论：Phase 3A 已按限定边界完成并提交；MEDIUM/LOW 发现保留为非阻断后续项。Phase 3B 随后已单独授权并完成；Phase 3C、3D 继续未授权。

## Phase 3B 收口核验

核验日期：2026-07-25。

| 项目 | 实际结果 |
| --- | --- |
| Git 分支 | `phase/03-import-foundation` |
| Phase 3B 实现提交 | `150194c feat: complete phase 3b import parsing preview mapping` |
| Phase 3B 结论 | 独立顶层 Evaluator 最终 PASS；`BLOCKER=0`、`HIGH=0` |
| 支持格式 | TXT、CSV、XLS、XLSX |
| 解析与预览 | 编码/分隔符识别及人工覆盖、Excel 工作表/表头选择、前 50 行预览、解析错误展示 |
| 字段映射 | 服务端数据集字段/转换定义、映射保存、可复用且版本不可变的映射模板 |
| 数据库不变量 | 模板版本配置和数据集冻结；批次/Workspace 身份不可移动；预览失效与映射变更保持同一事务 |
| OpenAPI | multipart 文件字段为必填 binary，契约与实现一致 |
| Phase 3B 数据库迁移 | `202607250003` 至 `202607250007` 已在 `futures` VPS 执行 |
| 越界核验 | 未新增 confirm/events/rollback/cancel、任务队列、SSE、正式入库或 `import_row_changes` |

非阻断 MEDIUM 已记录到 `docs/reviews/PHASE_03B_EVALUATION.md`：同参数 inspect 的前端预览状态处理、errors API 分页、数据库回滚/冻结竞争测试补强及测试脚本前置注释更新。它们不回退 Phase 3B PASS，后续必须在适当阶段显式排期。

## 本地验证结果

最近验证时间：2026-07-25，Phase 3B 收口验证。

| 命令 | 结果 | 备注 |
| --- | --- | --- |
| `cargo +stable fmt --check` | 通过 | 在 `rust/` 目录执行 |
| `cargo +stable clippy --workspace --all-targets -- -D warnings` | 通过 | 无 warning |
| `cargo +stable test --workspace` | 通过 | 25 项测试通过 |
| `pnpm lint` | 通过 | `vue-tsc --noEmit` |
| `pnpm test` | 通过 | 前端测试通过 |
| `pnpm build` | 通过 | 生产构建完成 |
| `docker --version` | 未通过 | 本机未安装 Docker 或未加入 PATH |
| `docker compose config/build/up` | 本机未执行；VPS 通过 | 本机无 Docker；Docker 门禁在 `futures` VPS 完成 |

## futures VPS 核对结果

最近核对时间：2026-07-25，Phase 3B 部署验收。

| 项目 | 结果 |
| --- | --- |
| SSH 别名 | `futures` |
| 系统 | Ubuntu/Linux `7.0.0-22-generic`，x86_64 |
| Docker | Docker 29.1.3 |
| Docker Compose | Docker Compose 2.40.3 |
| 当前容器 | PostgreSQL、API、Worker、Frontend、Nginx 均运行；API/PostgreSQL healthy |
| 监听端口 | SSH 22、本项目 HTTP 8088、DNS stub 53、chrony 323 |
| 根分区 | Phase 3B 最终验收时：25G 总量、约 11G 已用、13G 可用，使用率 45% |
| 内存 | 约 956MiB；已启用项目 swapfile 2GiB |

部署状态：Phase 3B 已部署并验证。访问地址：`http://172.238.11.174:8088`。

Phase 3A VPS 证据：

- Compose config/build/up 通过，PostgreSQL、API、Worker、Frontend、Nginx 五个服务运行，API/PostgreSQL healthy。
- `schema_versions` 包含 Phase 3A 迁移 `202607250001`、`202607250002`。
- 30 MiB 上传返回 201；大于 50 MiB 返回 413。
- viewer 权限不足、CSRF 缺失和 Origin 不匹配均被拒绝并形成审计记录；未认证请求也验证为拒绝，但不计入 Workspace 审计。
- RLS 与跨 Workspace API/数据库隔离验证通过。
- 五轮上传的数据库记录、对象文件和 SHA-256 均为 5/5/5 一致。
- 原子写入临时文件残留 `.tmp=0`；日志秘密扫描命中数为 0。

Phase 3B VPS 证据：

- 最新 API 镜像构建完成，PostgreSQL、API、Worker、Frontend、Nginx 均运行；API/PostgreSQL healthy。
- `schema_versions` 包含 `202607250003` 至 `202607250007`。
- Phase 3B HTTP E2E 返回 `PHASE3B_E2E_PASS`。
- 映射数据库不变量 SQL 测试和双连接模板首次绑定并发测试通过。
- 最终四个映射/模板不变量触发器存在，模板版本 `dataset_type` 为 NOT NULL，测试夹具残留为 0。

本轮 VPS 容量治理：

- 清理前 `/`：25G 总量、21G 已用、2.0G 可用、92% 使用率。
- 清理后 `/`：25G 总量、9.1G 已用、14G 可用、40% 使用率。
- 清理范围：本项目 `/tmp` 临时部署包和 Docker build cache。
- 未删除 PostgreSQL 数据卷、对象存储卷或当前运行镜像。

## 验证限制

已完成 Word 文本、标题、表格、内嵌图片、批注和修订结构检查。当前环境缺少 LibreOffice，未能将 Word 文件逐页渲染为 PNG，因此未对分页、字体替换和表格跨页进行视觉验收；原文件未被修改。

## Phase 3 计划摘要

Phase 3 详细边界见 `docs/phases/PHASE_03_IMPORT_FOUNDATION.md`，按以下顺序独立准入和收口：

- Phase 3A：上传、文件存储抽象、文件哈希、`import_batches` 状态机。
- Phase 3B（已完成并 PASS）：TXT/CSV/XLS/XLSX 解析，编码/分隔符识别与人工覆盖，Excel 工作表/表头选择，前 50 行预览，字段映射和映射模板，解析错误展示，并修复 Phase 3A 遗留 OpenAPI multipart schema 契约问题。
- Phase 3C（未授权）：校验、去重、冲突策略、确认导入、PostgreSQL 任务队列、SSE。
- Phase 3D（未授权）：原子回滚、补偿批次、前端完整流程、VPS 部署验收。

明确排除：套利统计和图表、交易与持仓、外部网站采集、OCR、AI、自动回测。

Phase 3B 实施边界：

- 允许文件范围：`docs/` 可更新；Rust 仅限 `domain`、`application`、`database`、`infrastructure`、`api` import 相关模块；必要 `Cargo.toml`、`Cargo.lock`；新增 Phase 3B migration；`frontend/` 仅限导入中心 3B 最小页面/API/测试；`deploy/nginx` 仅在 multipart/body limit 契约需要时最小调整。
- API 边界：保留 `POST /api/v1/imports` 和 `GET /api/v1/imports/{import_id}`；可按需新增 `inspect`、`mapping`、`preview`、`import-templates`、只读 `errors`；不得新增 `confirm`、`events`、`rollback`、`cancel` 或 job semantics。
- DB 边界：允许 templates、template_versions、mappings、staging preview、errors metadata；不得新增 row_changes、jobs、正式业务目标表写入或冲突策略写入。
- frontend 边界：仅展示 3B inspect/mapping/preview/template/errors 最小流程；不得暴露确认导入、任务进度、SSE、取消、回滚或补偿。
- 验收标准：四类文件正常/边界/恶意样例、编码/分隔符人工覆盖、Excel 工作表/表头选择、前 50 行上限、模板版本不可变、错误脱敏展示、OpenAPI multipart 契约一致、跨 Workspace API/RLS 隔离、无 3C/3D 越界实现。
- 测试门槛：本地 Rust fmt/test/clippy 和前端 lint/test/build 通过；VPS 完成适用 Docker、迁移、OpenAPI、四类文件 inspect/mapping/preview、错误展示、跨 Workspace API/RLS、日志秘密扫描；Evaluator PASS 且无 BLOCKER/HIGH。

## 后续步骤

1. 保留 Phase 3B Evaluator 的 MEDIUM 发现为非阻断后续项，不回退 Phase 3B PASS。
2. Phase 3C 仍未授权；如需继续，必须先由 Planner 单独确认校验、去重、冲突策略、确认导入、PostgreSQL 任务队列和 SSE 的范围。
3. Phase 3D 继续未授权、未实现；不得提前新增回滚、补偿批次或完整前端流程。
4. 若要进入生产 HTTPS，需要先补齐 TLS 入口与 `AUTH_COOKIE_SECURE=true` 的生产部署验证。

## 变更规则

- 新需求先进入产品需求和待确认事项，不直接进入实现。
- 业务口径变化必须同步更新数据库、API、测试样例和验收标准。
- 对原始数据、公式、分类和映射的变更必须保留版本与迁移策略。

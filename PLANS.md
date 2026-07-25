# 项目计划状态

## 当前阶段

阶段 3：导入基础（计划待确认）。

状态：Phase 1 工程基础建设已完成，Evaluator 最终 PASS。Phase 2 已按 `docs/phases/PHASE_02_IDENTITY_WORKSPACE_SECURITY.md` 实施、测试、部署到 `futures` VPS，并经最终 Evaluator 审查 PASS。版本收口已完成：`phase/01-foundation` 合并到 `main`，标签 `phase-2-pass-20260725` 已创建，并已从 `main` 创建当前分支 `phase/03-import-foundation`。本轮只制定 Phase 3 导入基础计划，不开始实现，不调用 Generator，不修改已经 PASS 的 Phase 1、Phase 2 实现。

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
| Phase 3 Planner 计划 | 待确认 | `docs/phases/PHASE_03_IMPORT_FOUNDATION.md`；仅规划导入基础，不实现业务代码 |

## 本轮接管核验

核验日期：2026-07-25。

| 项目 | 实际结果 |
| --- | --- |
| Git 分支 | `phase/03-import-foundation` |
| Git 工作区 | 接管核验时干净；本轮仅允许新增 Phase 3 计划文档并同步计划状态 |
| Git HEAD | `6dfea78 feat: complete phase two identity foundation` |
| 最近提交 | `6dfea78`、`95ed601`、`146a614`、`81308ed`、`0ee8671` |
| 版本收口 | `main`、`phase/01-foundation`、`phase/03-import-foundation` 和标签 `phase-2-pass-20260725` 均指向 `6dfea78` |
| Phase 1 结论 | Evaluator 最终 PASS |
| Phase 2 结论 | Evaluator 最终 PASS |
| VPS 容器 | PostgreSQL、API、Worker、Frontend、Nginx 均运行 |
| VPS 健康 | API/PostgreSQL healthy；live/ready/version/首页/Nginx 代理 HTTP 200 |
| 当前部署版本 | `0.1.0`；`git_sha=local` |
| 数据库迁移 | `202607240001`：`phase 1 foundation`；`202607240002`：`phase 2 identity workspace security` |
| RLS 基础 | `audit_logs` 已启用并强制 RLS；运行时角色为 `NOSUPERUSER`、`NOBYPASSRLS` |
| 源码一致性 | VPS 部署目录来自源码包 overlay，远端不保留 `.git`，以本地 Git 为唯一源码源头 |

接管结论：Phase 2 PASS 交接事实与 Git、迁移和 VPS 实态一致；可以进入 Phase 3 规划确认，但尚未开始 Phase 3 实现。

## 本地验证结果

最近验证时间：2026-07-25 01:13 +08:00。

| 命令 | 结果 | 备注 |
| --- | --- | --- |
| `cargo +stable fmt --check` | 通过 | 在 `rust/` 目录执行 |
| `cargo +stable clippy --workspace --all-targets -- -D warnings` | 通过 | 无 warning |
| `cargo +stable test --workspace` | 通过 | 包含 Phase 2 认证基础单元测试和登录限速阈值测试 |
| `pnpm install --frozen-lockfile` | 通过 | 锁文件已生成并校验 |
| `pnpm lint` | 通过 | `vue-tsc --noEmit` |
| `pnpm test` | 通过 | 沙箱内被 esbuild 上级目录读取权限拦截；使用真实文件系统权限重跑通过 |
| `pnpm build` | 通过 | 沙箱内同样受 esbuild 权限拦截；使用真实文件系统权限重跑通过；存在 Vite 大 chunk 提示，作为 LOW 记录 |
| `docker --version` | 未通过 | 本机未安装 Docker 或未加入 PATH |
| `docker compose config` | 本机未通过；VPS 通过 | 本机无 Docker；`futures` VPS 已执行 `docker compose --profile dev config --quiet` |

## futures VPS 核对结果

最近核对时间：2026-07-25 11:35 +08:00。

| 项目 | 结果 |
| --- | --- |
| SSH 别名 | `futures` |
| 系统 | Ubuntu/Linux `7.0.0-22-generic`，x86_64 |
| Docker | Docker 29.1.3 |
| Docker Compose | Docker Compose 2.40.3 |
| 当前容器 | PostgreSQL、API、Worker、Frontend、Nginx 均运行；API/PostgreSQL healthy |
| 监听端口 | SSH 22、本项目 HTTP 8088、DNS stub 53、chrony 323 |
| 根分区 | 25G，部署后约 14G 可用 |
| 内存 | 约 956MiB；已启用项目 swapfile 2GiB |

部署状态：Phase 2 已部署。访问地址：`http://172.238.11.174:8088`。

Phase 2 VPS 证据：

- `docker compose --profile dev config --quiet`、build、`up -d --force-recreate` 通过。
- `/api/v1/health/live`、`/api/v1/health/ready`、`/api/v1/version`、`/api-docs/openapi.json` 返回 HTTP 200。
- `schema_versions` 记录 `202607240001` 和 `202607240002`。
- `futures_runtime` 为 `NOSUPERUSER`、`NOBYPASSRLS`、`CANLOGIN`。
- `audit_logs` 启用并强制 RLS；跨 Workspace 读取/写入破坏性验证通过。
- 认证 E2E 覆盖 login、me、workspace、csrf、logout、Session 上限和跨用户撤销拒绝。
- 登录限速审计已验证：10 次失败写入 `failure`，第 11 次 429 写入 `denied`。
- bootstrap token 宿主文件已删除；secret 日志扫描未发现敏感值。
- 最新部署包本地与远端 SHA-256 均为 `52c0577d2aef38bf2126f6b78d004e13af183683ac72daf7c45b16dd3e805230`。

## 验证限制

已完成 Word 文本、标题、表格、内嵌图片、批注和修订结构检查。当前环境缺少 LibreOffice，未能将 Word 文件逐页渲染为 PNG，因此未对分页、字体替换和表格跨页进行视觉验收；原文件未被修改。

## Phase 3 计划摘要

Phase 3 建议范围详见 `docs/phases/PHASE_03_IMPORT_FOUNDATION.md`。核心交付为：

- TXT、CSV、XLS、XLSX 上传。
- 原始文件本地 `ObjectStorage` 抽象。
- `import_batches`、文件、模板、映射、staging、错误和变更日志。
- 编码、分隔符、工作表、表头识别。
- 字段映射模板和前 50 行预览。
- 数据校验、错误报告、去重和冲突策略。
- 整批原子回滚和补偿批次。
- Workspace 隔离、PostgreSQL RLS、审计。
- SSE 导入进度。
- 本地测试与 `futures` VPS 验收流程。

明确排除：套利统计和图表、交易与持仓、外部网站采集、OCR、AI、自动回测。

## 后续步骤

1. 等待项目所有者确认 `docs/phases/PHASE_03_IMPORT_FOUNDATION.md`。
2. 确认后再按 Planner → Generator → 本地测试 → futures VPS 验收 → Evaluator → 修复 → 复核 PASS → Git 提交 → handoff 的流程实施。
3. 实施前不得调用 Generator，不得编写导入业务代码。
4. 若要进入生产 HTTPS，需要先补齐 TLS 入口与 `AUTH_COOKIE_SECURE=true` 的生产部署验证。

## 变更规则

- 新需求先进入产品需求和待确认事项，不直接进入实现。
- 业务口径变化必须同步更新数据库、API、测试样例和验收标准。
- 对原始数据、公式、分类和映射的变更必须保留版本与迁移策略。

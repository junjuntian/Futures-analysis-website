# 项目计划状态

## 当前阶段

阶段 2：身份、个人 Workspace 与隔离基础（完成收口）。

状态：Phase 1 工程基础建设已完成，Evaluator 最终 PASS。Phase 2 已按 `docs/phases/PHASE_02_IDENTITY_WORKSPACE_SECURITY.md` 实施、测试并部署到 `futures` VPS，最终 Evaluator 审查 PASS。本阶段只建设首次用户初始化、个人 Workspace、Cookie Session、CSRF、权限基础、审计与 PostgreSQL RLS 双层隔离，未进入目录、导入、行情、交易、AI、OCR 或外部数据采集。原始 Word 设计方案未修改。

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

## 本轮接管核验

核验日期：2026-07-24。

| 项目 | 实际结果 |
| --- | --- |
| Git 分支 | `phase/01-foundation` |
| Git 工作区 | 接管核验时干净；Phase 2 实施产生代码、配置、迁移、文档和前端变更 |
| Git HEAD | `95ed601 docs: add agent handoff checkpoint` |
| 最近提交 | `95ed601`、`146a614`、`81308ed`、`0ee8671` |
| Phase 1 结论 | Evaluator 最终 PASS |
| VPS 容器 | PostgreSQL、API、Worker、Frontend、Nginx 均运行 |
| VPS 健康 | API/PostgreSQL healthy；live/ready/version/首页/Nginx 代理 HTTP 200 |
| 当前部署版本 | `0.1.0`；`git_sha=local` |
| 数据库迁移 | 接管时仅 `202607240001`：`phase 1 foundation` |
| RLS 基础 | 接管时 `app.current_workspace_id()` 存在；尚无正式业务表 |
| 源码一致性 | 关键部署文件哈希与本地一致 |

接管结论：Phase 1 交接事实与 Git、迁移和 VPS 实态一致，可以进入 Phase 2 实施。

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

最近核对时间：2026-07-25 01:13 +08:00。

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

## 后续步骤

1. 提交 Phase 2 收口变更。
2. 后续 Phase 3 前必须重新由 Planner 明确范围，不得直接进入行情、导入、交易、AI、OCR 或外部数据采集。
3. 若要进入生产 HTTPS，需要先补齐 TLS 入口与 `AUTH_COOKIE_SECURE=true` 的生产部署验证。

## 变更规则

- 新需求先进入产品需求和待确认事项，不直接进入实现。
- 业务口径变化必须同步更新数据库、API、测试样例和验收标准。
- 对原始数据、公式、分类和映射的变更必须保留版本与迁移策略。

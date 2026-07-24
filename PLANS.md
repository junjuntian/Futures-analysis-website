# 项目计划状态

## 当前阶段

阶段 1：工程基础建设。

状态：剩余 P0 技术决策已写入 `docs/DECISIONS.md`；Phase 0 文档审查已使用 `ce-doc-review` 完成并修正发现的问题；Phase 1 计划已创建；Git 仓库、Rust/Vue/Docker/Nginx/CI 基础工程骨架已落地。本地 Rust 与前端验证已通过；Docker/Compose 在本机不可用，`futures` VPS 也未安装 Docker，因此容器部署验证暂时阻塞。原始 Word 设计方案不得修改。

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
| Phase 1 工程实现 | 基本完成 | 已创建 Git、Rust/Vue/Docker/Nginx/CI 基础文件；Rust 与前端验证通过；Docker 配置因本机无 Docker 未验证 |
| futures VPS 部署验证 | 阻塞 | 已通过 MCP SSH 核对；`futures` VPS 未安装 Docker/Compose，无法执行容器部署验证 |

## 本地验证结果

验证时间：2026-07-24 20:59 +08:00。

| 命令 | 结果 | 备注 |
| --- | --- | --- |
| `cargo +stable fmt --check` | 通过 | 在 `rust/` 目录执行 |
| `cargo +stable clippy --workspace --all-targets -- -D warnings` | 通过 | 无 warning |
| `cargo +stable test --workspace` | 通过 | 当前为基础骨架测试，未包含业务测试 |
| `pnpm install --frozen-lockfile` | 通过 | 锁文件已生成并校验 |
| `pnpm lint` | 通过 | `vue-tsc --noEmit` |
| `pnpm test` | 通过 | 沙箱内被 esbuild 上级目录读取权限拦截；使用真实文件系统权限重跑通过 |
| `pnpm build` | 通过 | 沙箱内同样受 esbuild 权限拦截；使用真实文件系统权限重跑通过；存在大 chunk 提示，Phase 1 可接受 |
| `docker --version` | 未通过 | 本机未安装 Docker 或未加入 PATH |
| `docker compose config` | 未通过 | 同上，无法校验 Compose 配置 |

## futures VPS 核对结果

核对时间：2026-07-24 20:59 +08:00。

| 项目 | 结果 |
| --- | --- |
| SSH 别名 | `futures` |
| 系统 | Ubuntu/Linux `7.0.0-22-generic`，x86_64 |
| Docker | 未安装：`docker: command not found` |
| Docker Compose | 未安装：`docker-compose: command not found` |
| 当前容器 | 无法查询；Docker 不可用 |
| 监听端口 | 仅观察到 SSH 22、DNS stub 53、chrony 323 等基础端口 |
| 根分区 | 25G，总用量约 2.7G，约 21G 可用 |
| 内存 | 约 956MiB，总可用约 762MiB |

部署状态：尚未部署。阻塞原因是 VPS 缺少 Docker/Compose；是否安装 Docker 或改用其他部署方式需要用户确认。

## 验证限制

已完成 Word 文本、标题、表格、内嵌图片、批注和修订结构检查。当前环境缺少 LibreOffice，未能将 Word 文件逐页渲染为 PNG，因此未对分页、字体替换和表格跨页进行视觉验收；原文件未被修改。

## 后续步骤

1. 等待用户确认是否允许在 `futures` VPS 安装 Docker/Compose，或指定其他部署方式。
2. 用户确认后，执行容器部署验证并更新 `docs/deployments/FUTURES_VPS_PHASE_01.md`。
3. 在容器验证完成后，进行 Phase 1 审查与修复。
4. 若需要切换上下文，按 `docs/handoffs/README.md` 创建交接文档。

## 变更规则

- 新需求先进入产品需求和待确认事项，不直接进入实现。
- 业务口径变化必须同步更新数据库、API、测试样例和验收标准。
- 对原始数据、公式、分类和映射的变更必须保留版本与迁移策略。

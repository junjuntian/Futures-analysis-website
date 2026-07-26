# 项目计划状态

## 当前阶段

阶段 3：导入基础；Phase 3A、Phase 3B、Phase 3C 均已完成并经独立 Evaluator PASS；Phase 3D 范围与关键决策已由用户确认并获实施授权，Generator 尚未开始。

状态：Phase 1、Phase 2 均已完成并经 Evaluator PASS。Phase 3 已拆为 3A、3B、3C、3D。Phase 3A 以 `1b089f7 feat: complete phase 3a import upload foundation` 收口。Phase 3B 以 `150194c feat: complete phase 3b import parsing preview mapping` 收口。Phase 3C 实现提交为 `04011ed feat: complete phase 3c validation and async import`，收口提交为 `6e1d46d`，独立 Evaluator 最终 `PASS`、`BLOCKER=0`、`HIGH=0`。Phase 3D 唯一详细契约为 `docs/phases/PHASE_03D_IMPORT_FINALIZATION.md`，已拆为可交给 Generator 的小任务包。

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
| Phase 3C：校验与异步确认 | 已完成、已提交、Evaluator PASS | 实现提交 `04011ed`，收口提交 `6e1d46d`；独立 Evaluator `BLOCKER=0`、`HIGH=0` |
| Phase 3D：回滚与完整流程 | 已确认、已授权、待 Generator 实施 | API 同步全量预检并幂等入队，Worker 异步原子回滚，冲突整体中止，补偿/lineage，对象 scan/check/quarantine/audit，完整前端与最终验收 |
| 标准发布流程 | 已确认 | 本地 Git → GitHub 私有仓库 → Codex Cloud/GitHub Actions 测试 → `linux/amd64` 镜像 → GHCR → `futures` VPS pull/迁移/E2E；VPS 不再承担常规编译 |
| Phase 3D 云端准入 | 已通过 | SHA `636c8ae036f6ea65e8292bca19f38205db98f4a6`；CI run `30187416767` success；Container images run `30187946869` success |

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

收口结论：Phase 3A 已按限定边界完成并提交；其未关闭 MEDIUM 已明确归入 Phase 3D。Phase 3B、Phase 3C 随后均已单独授权、完成并经 Evaluator PASS；Phase 3D 已确认并获实施授权。

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

非阻断 MEDIUM 已记录到 `docs/reviews/PHASE_03B_EVALUATION.md`，实际共五项而非三项；它们不回退 Phase 3B PASS，并全部纳入 Phase 3C：①同参数 inspect 的前端预览/errors 状态为前端前置修复；②errors API 固定 500 条改为稳定游标分页；③映射写入失败后的 staging/errors/status 一致性为普通数据库事务回归测试，不是批次回滚功能；④模板 `dataset_type` 冻结竞争为并发数据库回归测试；⑤两份脚本的迁移前置注释由 006 更新为实际依赖的 007。

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
- Phase 3C（已完成并 PASS）：校验、业务唯一性、`skip`/`overwrite`/`keep_conflict`/`abort`、通用 `imported_records` 正式入库、PostgreSQL `job_queue`、Worker 租约/重试/dead-letter、并发确认与幂等、SSE `Last-Event-ID` 重放、Workspace/RLS/审计及最小确认/进度 UI。
- Phase 3D（已确认、已授权、待实施）：API 同步全量预检和幂等入队、Worker 异步单事务原子回滚、后续修改/依赖冲突整体中止、补偿批次、审计与来源链、对象 scan/check/quarantine/audit、完整前端、全量自动化及 VPS/Evaluator 最终验收。详细实施契约见 `docs/phases/PHASE_03D_IMPORT_FINALIZATION.md`。

明确排除：套利统计和图表、交易与持仓、席位分析、外部网站采集、浏览器识别、OCR、AI、自动回测。

Phase 3C 实施边界：

- 以 `docs/phases/PHASE_03_IMPORT_FOUNDATION.md` 第 8.3 节为唯一详细实施契约；正式目标仅为导入域通用 `imported_records`，`keep_conflict` 只把候选保存在 `import_conflict_candidates`，不是 `keep_both`。
- `/confirm` 必须在单事务内冻结参数、记录幂等请求、唯一入队、写首事件和审计；同键重试返回原结果，并发确认收敛到同一 job。
- Worker 使用 PostgreSQL `FOR UPDATE SKIP LOCKED`、30 秒租约/10 秒续租、最多 5 次指数退避重试和终态 dead-letter；SSE 使用持久化批次序号与 `Last-Event-ID` 精确重放。
- cancel、人工 dead-letter replay、回滚、补偿、`import_row_changes`、冲突人工解决和完整前端继续延期；行情、交易、套利、图表、外部采集、OCR、AI 与回测继续禁止。
- 本地完整回归、VPS Docker/迁移/四策略 E2E/并发确认/Worker 恢复/SSE 重放/跨 Workspace RLS/审计秘密扫描及独立 Evaluator PASS 均为退出门禁。

## Phase 3D 授权状态与实施顺序

1. Phase 3D Planner 契约已由用户确认并获实施授权：`docs/phases/PHASE_03D_IMPORT_FINALIZATION.md`。
2. 云端准入已通过：CI run `30187416767`、Container images run `30187946869`，目标 SHA 为 `636c8ae036f6ea65e8292bca19f38205db98f4a6`。该证据只覆盖实施前基线，Phase 3D 候选提交必须重新运行云端门禁。
3. Generator 依次执行八个小任务包：契约/迁移 → 正式导入 change log → 回滚预检/入队 API → 回滚 Worker → 补偿/lineage → 对象一致性 → 前端/MEDIUM → 全量门禁/VPS/Evaluator/收口。
4. API 必须同步全量预检并幂等创建唯一 `import_rollback` job；Worker 异步执行并在事务内重验。成功时全部逆变更、数据失效、批次/request/job 状态、事件和审计单事务提交；冲突时正式目标零变更并整体中止。
5. Phase 3C 已成功批次无完整 change log，禁止伪造 backfill。只有带 `rollback_capability`、`change_log_version` 或等价完整能力标记的新批次可直接回滚；旧批次以及存在后续修改/依赖的批次只允许可追溯补偿。
6. 对象治理只允许 scan、check、quarantine 和 audit，Phase 3D 绝不物理删除。
7. 明确排除 `cancel`、人工 dead-letter replay、冲突候选人工合并、套利/交易/持仓/席位/图表、外部采集、浏览器识别、OCR、AI 和自动回测。
8. Phase 3A 仍需归入 3D 的 MEDIUM 为对象孤儿治理和可重复 API/DB/RLS 自动化；multipart 契约已由 3B 关闭。Phase 3B 的 5 项 MEDIUM 已由 3C 全部关闭。Phase 3C 的 4 项 MEDIUM 全部纳入 3D：SSE OpenAPI 帧 schema、跨 Workspace 公平轮询、长连接周期性重验会话、确认面板/断线重连组件级测试。
9. Phase 3D 候选镜像由 GitHub Actions 发布到 GHCR；`futures` VPS 只按 digest pull、备份、迁移、验证 RLS/对象持久化和最终 E2E，不上传源码常规 build。
10. 独立 Evaluator PASS 后使用普通 merge commit 合并 `main`，禁止 squash/rebase；创建实际完成日期 `phase-3-pass-YYYYMMDD`，不创建 `v*`。
11. 若要进入生产 HTTPS，仍需补齐 TLS 入口与 `AUTH_COOKIE_SECURE=true` 的生产部署验证；该事项不属于 Phase 3D 授权。

## 已确认的标准发布流程

确认日期：2026-07-26。

- 本地 Git 仓库是唯一源码源头，所有变更先提交再推送 GitHub 私有仓库。
- Codex Cloud / GitHub Actions 承担云端编译测试；GitHub Actions 构建并发布
  `linux/amd64` API、Worker、前端镜像到 GHCR。
- `futures` VPS 禁止直接修改源码，不再承担常规 Rust 或前端编译，只执行数据库
  备份、`docker pull`、数据库迁移、真实 RLS/持久化和最终 E2E 验收。
- 部署只能引用 SHA 标签或完整 digest，不能只使用 `latest`。
- 生产部署前必须备份数据库；失败时按发布记录中的上一稳定镜像 digest 回滚。
- 生产密钥不得进入 Git、镜像层、构建日志、构建参数或普通环境变量文件。
- GitHub Actions / Codex Cloud 通过不能替代 `futures` VPS 验收。
- GHCR 工作流已在 SHA `636c8ae036f6ea65e8292bca19f38205db98f4a6` 成功；只有用户提供只读拉取凭据且 Phase 3D 候选镜像成功后，才切换 `futures` VPS 到 digest pull 部署方式。

## 变更规则

- 新需求先进入产品需求和待确认事项，不直接进入实现。
- 业务口径变化必须同步更新数据库、API、测试样例和验收标准。
- 对原始数据、公式、分类和映射的变更必须保留版本与迁移策略。

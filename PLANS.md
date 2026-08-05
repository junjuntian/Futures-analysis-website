# 项目计划状态

## 当前阶段

Phase 3 已全部完成并收口。Phase 4A 已通过独立终验与 MEDIUM-05 轻量复确认并完成阶段性收口：普通 merge commit `1884583` 已合入 `main`，main CI Run `30990641425` success，标签为 `phase-4a-pass-20260805`。Phase 4B-1 在 main 基线记录为 VPS 自治运行，Phase 4B-2 尚未启动；本 Planner 单不核验或改变其运行实态，用户当前指令为回填保持暂停，恢复须另行授权。Phase 5 已从当前 `main` 新建 `phase/05-spread-analytics`，当前只编写修订版 Planner 契约并等待用户确认，未进入实现。

状态：Phase 1、Phase 2、Phase 3 均已完成并经 Evaluator PASS。Phase 3A 以 `1b089f7` 收口，Phase 3B 以 `150194c` 收口，Phase 3C 实现提交为 `04011ed`、收口提交为 `6e1d46d`。Phase 3D 实现与部署完成后，首轮独立 Evaluator 在 `12716d2` 判定 FAIL（HIGH 2 / MEDIUM 5 / LOW 1）；修复序列为 `bf5baab`、`1b4c06b`、`f13b17d`、`3aa200c`、`5b67393`、`1c32ab4`、`49fc930`、`c87b26f`、`45ee802`，复核提交 `6ff4c2b` 最终 PASS。`main` 普通 merge commit 为 `33aa838c9ef3f39c4e32bb5749982d82d358bf7a`，main CI Run `30703979390` success，标签为 `phase-3-pass-20260801`。总方案重审废止 `DEC-023`、`DEC-030`、`DEC-034`，修订 `DEC-025`、`DEC-031`，新增 `DEC-038` 至 `DEC-040`；Phase 4A 追加 `DEC-041`；Phase 5 Planner 新增有限恢复三禾只读价差 API 的 `DEC-042`。Phase 4A 残留 HIGH-03 修复为 `23e679d`，self-hosted 工作流适配为 `4e39c69`、`d4265c4`、`e627ab8`；候选 `e627ab8` 的 CI Run `30969365344`、四镜像 Run `30970280360`、Deploy Run `30971024520` 全部 success，VPS 返回 `PHASE4A_E2E_PASS`。MEDIUM-05 修复 `8018f32` 通过 CI Run `30977655724` 与 VPS loopback 监听取证，轻量复确认 `814a09a` 未发现新 HIGH；Phase 4A 随后以普通 merge `1884583`、main CI `30990641425` 和标签 `phase-4a-pass-20260805` 阶段性收口，未重新部署 VPS。

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
| 总方案重审决策同步 | 已完成 | 采集域裁剪、akshare、自动采集免确认、全历史回填和前端图表导出已落入 `DEC-023/025/030/031/034/038/039/040` 及各设计文档 |
| Phase 5 修订版 Planner 契约 | 待用户确认、未实施 | `docs/phases/PHASE_05_SPREAD_ANALYTICS.md`；`DEC-042`；5A/5B 分别设独立 Evaluator 门禁 |
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
| Phase 3 分支创建 | 已完成、使命结束 | `phase/03-import-foundation` 从 Phase 2 基线创建，最终 HEAD `6ff4c2b` 已合入 `main`，不再回灌状态文档 |
| Phase 3 Planner 拆分 | 已完成 | `docs/phases/PHASE_03_IMPORT_FOUNDATION.md` 已明确 3A/3B/3C/3D 的边界、门禁和退出条件 |
| Phase 3A：上传与批次基础 | 已完成、已提交、Evaluator PASS | 上传、文件存储抽象、文件哈希、`import_batches` 状态机已实现；提交 `1b089f7`，Evaluator `BLOCKER=0`、`HIGH=0` |
| Phase 3B：解析、预览与映射 | 已完成、已提交、Evaluator PASS | TXT/CSV/XLS/XLSX 解析、编码/分隔符识别与人工覆盖、Excel 工作表/表头选择、前 50 行预览、字段映射与版本模板、错误展示、OpenAPI multipart schema 契约修复；提交 `150194c`，Evaluator `BLOCKER=0`、`HIGH=0` |
| Phase 3C：校验与异步确认 | 已完成、已提交、Evaluator PASS | 实现提交 `04011ed`，收口提交 `6e1d46d`；独立 Evaluator `BLOCKER=0`、`HIGH=0` |
| Phase 3D：回滚与完整流程 | 已完成、Evaluator 复核 PASS | 原子回滚、冲突整体中止、补偿/lineage、对象治理、完整前端和全量自动化已完成；候选 `45ee802` 已部署并通过 VPS 验收，复核 `6ff4c2b` PASS |
| 标准发布流程 | 已确认并实际运行 | `futures-vps` self-hosted Actions 在 2.5 GiB 总峰值护栏内发布四镜像；Deploy Run `30971024520` 按完整 digest 完成备份、迁移和 E2E，运行版本 `e627ab8c3b797cc77f872a9c02439c1dfca0d4eb` |
| Phase 3 版本收口 | 已完成 | 普通 merge `33aa838`；main CI Run `30703979390` success；标签 `phase-3-pass-20260801`；标签镜像 Run `30704223198` success；未重新部署 VPS |
| Phase 4A：akshare 单日采集链路 | 已完成、Evaluator PASS、已阶段性收口 | 首轮 FAIL（H5/M4/L1）经修复、9/10 复核、HIGH-03 终验和 MEDIUM-05 复确认后计数归零；普通 merge `1884583`、main CI `30990641425`、标签 `phase-4a-pass-20260805` |
| Phase 4B：全历史回填 | 4B-1 当前按用户指令保持暂停、运行实态待另单只读核验；4B-2 未启动 | 不在 Phase 5 Planner 单启动、停止或修改；恢复须另行授权，既有 80 日上限、60 秒限速、保护窗、磁盘 80% 护栏不变 |
| Phase 5–9 路线 | Phase 5 Planner 契约待确认、实现未授权；Phase 6–9 未实施 | Phase 5 拆为 5A 三禾自由价差和 5B 自有库套利监控，各自独立 Evaluator；席位接口仍留 Phase 7，不得操作 Phase 4B 回填 |

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

收口结论：Phase 3A 已按限定边界完成并提交；其遗留 MEDIUM 随后在 Phase 3B 至 3D 中关闭。Phase 3B、Phase 3C、Phase 3D 均已单独授权、完成并经 Evaluator PASS；Phase 3 整体已合入 `main` 并打标签收口。

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

## Phase 4A 阶段性收口

收口日期：2026-08-05。

| 项目 | 实际结果 |
| --- | --- |
| 最终评审 | `docs/reviews/PHASE_04A_EVALUATION.md`；首轮 H5/M4/L1、HIGH-03 残留及新增 MEDIUM-05 均已关闭，轻量复确认提交 `814a09a` 未发现新 HIGH |
| 普通 merge | `1884583035798436173c71af5d1225048dcf8633`；父提交为 `2a6e6d5` 与 `814a09a`，未使用 squash/rebase |
| 冲突处理 | 无冲突；合并后 CI、container-images、deploy-futures workflow 与 phase 分支完全一致 |
| main CI | Run `30990641425` success；validate 及 API/Worker/Frontend/Collector 五个 job 全部 success |
| 标签 | `phase-4a-pass-20260805`，精确指向 merge commit `1884583`；未创建 `v*` 标签 |
| VPS | 本单未部署；继续运行已验收候选 `e627ab8c3b797cc77f872a9c02439c1dfca0d4eb` |
| Phase 4B-1 | 连续五年回填保持自治运行；最终核验发现旧 `nohup` 进程在保护窗等待期无结束事件退出，内核与 runner 均无 OOM；2026-08-05 17:17:32 CST 以相同参数恢复为独立 `futures-backfill-phase4b1.service`，水位未回退且幂等跳过已完成日期 |
| 后续并行 | Phase 5 获准从收口后的 `main` 新建分支并行开发；Phase 4B-2 仍待另单 |

收口结论：Phase 4A 已具备独立版本锚点，可作为 Phase 5 并行开发基线；Phase 4B-1
继续作为数据操作任务运行。旧 `nohup` 进程的无记录退出未造成数据或水位回退，已切换到
独立 systemd transient service，避免 SSH 会话生命周期影响后续保护窗等待。

## 本地与云端验证结果

最近验证时间：2026-08-05，Phase 4A main merge commit `1884583`。

| 命令 | 结果 | 备注 |
| --- | --- | --- |
| `cargo +stable fmt --check` | 通过 | 在 `rust/` 目录执行 |
| `cargo +stable clippy --workspace --all-targets -- -D warnings` | 通过 | 无 warning |
| `cargo +stable test --workspace` | 通过 | main CI Run `30990641425` success，包含 HIGH-03 PostgreSQL 集成测试 |
| `pnpm lint` | 通过 | `vue-tsc --noEmit` |
| `pnpm test` | 通过 | 6 files / 18 tests 通过 |
| `pnpm build` | 通过 | 1468 modules transformed；仅有既存的大 chunk warning |
| `ruff check` / `ruff format --check` | 通过 | 固定 Python 容器只读运行，15 files already formatted |
| `pytest collector/tests -q` | 通过 | 35 passed |
| `git diff --check` | 通过 | 当前工作区 exit 0 |
| `docker --version` | 未通过 | 本机未安装 Docker 或未加入 PATH |
| `docker compose config/build` | 本机未执行；GitHub Actions 通过 | main CI Run `30990641425` 的 Compose config 与四个非发布构建 job success；部署候选的 Container images Run `30970280360` success |

## futures VPS 核对结果

最近核对时间：2026-08-05，Phase 4A Deploy Run `30971024520` 实时部署与 E2E。

| 项目 | 结果 |
| --- | --- |
| SSH 别名 | `futures` |
| 系统 | Ubuntu/Linux `7.0.0-22-generic`，x86_64 |
| Docker | Docker 29.1.3 |
| Docker Compose | Docker Compose 2.40.3 |
| 当前容器 | PostgreSQL、API、Worker、Frontend、Nginx 均运行；API/PostgreSQL healthy |
| 当前运行版本 | `e627ab8c3b797cc77f872a9c02439c1dfca0d4eb` |
| 数据库迁移 | Phase 3D `202607260001`、`202607260002` 与 Phase 4A `202608020001` 至 `202608030003` 已执行 |
| 受保护数据实态 | `users=32`、手动 `import_batches=144`；E2E 前后手动批次数与整行指纹不变 |
| 监听端口 | SSH 22、本项目 HTTP 8088、DNS stub 53、chrony 323 |
| 根分区 | 阶段性收口核对时使用率 25%；低于 80% 水位，包含 main CI 与标签镜像构建缓存增长 |
| 内存与 runner | VPS 4 GiB；runner `MemoryMax=2500M`，本轮 `oom=0`、`oom_kill=0` |

部署状态：Phase 4A 候选 `e627ab8` 已通过 Deploy Run `30971024520` 按四个 GHCR 完整 digest 部署并验证，VPS 返回 `PHASE4A_E2E_PASS` 与 `DEPLOYMENT_PASS`；独立终验及 MEDIUM-05 轻量复确认均已完成，Phase 4A 最终 PASS。本次阶段性收口只合并 Git、运行 main CI 并打标签，没有重新部署，VPS 继续运行 `e627ab8`。

Phase 4A VPS 与发布证据：

- CI Run `30753685223`、Container images Run `30753724067`、Deploy Run `30754021926` 全部 success。
- 真实日期 `2026-07-30` 的五交易所目录、日历、行情、席位采集全部成功；DCE 官方请求失败后仅 DCE 激活 `akshare_sina_dce_fallback`，正式行情/席位 `source_id` 指向真实聚合来源，其余四家保持官方来源。
- `market_prices`、`seat_positions` 均大于 0，业务唯一键重复为 0；同日完整重跑后正式表行数不变。
- 故障注入使 DCE 行情批次进入 `failed`，SHFE/CZCE/GFEX/CFFEX 四个行情批次仍 `succeeded`，正式行情行数不变。
- RLS、import batch / imported record / 正式事实表来源链、目录自动建档与稳定用户/手动批次保护均 PASS。
- host cron 已安装工作日 17:30、21:30 两次调度并使用 `flock`；collector cgroup 实测峰值 `130641920` bytes，小于 `512m` 限制。
- 临时 GHCR 认证清理 PASS；collector 凭据文件权限/只读挂载检查通过，日志秘密模式扫描无命中；Phase 3C/3D 生产 E2E 未重跑，避免新增手动测试批次。

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

Phase 3C/3D VPS 与发布证据：

- 候选 SHA `45ee8028647a1b8e4b8cda043e8012b4e281d739` 的 CI Run `30688920855`、Container images Run `30689138347`、Deploy Run `30689392268` 全部 success。
- API、Worker、Frontend 均以完整 GHCR digest 运行；版本接口返回完整候选 SHA，Phase 3C/3D E2E 与 Phase 3D schema invariants 均 PASS。
- Phase 3D 迁移 `202607260001`、`202607260002` 已执行；bootstrap-token absent，符合 DEC-026。
- 首轮 Evaluator FAIL 后的 HIGH-01、HIGH-02、MEDIUM-01 至 MEDIUM-04、LOW-01 已全部 CLOSED；MEDIUM-05 按用户裁定延后。
- Phase 3 合并后的 main CI Run `30703979390` 全部 success；标签触发的 Container images Run `30704223198` 三镜像发布 success。本次未触发 deploy-futures，VPS 继续运行 `45ee802`。

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
- Phase 3D（已完成并 PASS）：API 同步全量预检和幂等入队、Worker 异步单事务原子回滚、后续修改/依赖冲突整体中止、补偿批次、审计与来源链、对象 scan/check/quarantine/audit、完整前端、全量自动化及 VPS/Evaluator 最终验收均已完成。详细契约与验收依据见 `docs/phases/PHASE_03D_IMPORT_FINALIZATION.md` 和 `docs/reviews/PHASE_03D_EVALUATION.md`。

Phase 3 实施时明确排除：套利统计和图表、交易与持仓、席位分析、外部数据采集、AI、自动回测；后续范围以 2026-08-02 总方案重审决策为准。

Phase 3C 实施边界：

- 以 `docs/phases/PHASE_03_IMPORT_FOUNDATION.md` 第 8.3 节为唯一详细实施契约；正式目标仅为导入域通用 `imported_records`，`keep_conflict` 只把候选保存在 `import_conflict_candidates`，不是 `keep_both`。
- `/confirm` 必须在单事务内冻结参数、记录幂等请求、唯一入队、写首事件和审计；同键重试返回原结果，并发确认收敛到同一 job。
- Worker 使用 PostgreSQL `FOR UPDATE SKIP LOCKED`、30 秒租约/10 秒续租、最多 5 次指数退避重试和终态 dead-letter；SSE 使用持久化批次序号与 `Last-Event-ID` 精确重放。
- 当时的 Phase 3C 边界将 cancel、人工 dead-letter replay、回滚、补偿、`import_row_changes`、冲突人工解决和完整前端延期到 3D，并将行情、交易、套利、图表、外部采集、AI 与回测排除在 3C 之外。
- 本地完整回归、VPS Docker/迁移/四策略 E2E/并发确认/Worker 恢复/SSE 重放/跨 Workspace RLS/审计秘密扫描及独立 Evaluator PASS 均为退出门禁。

## Phase 3D 实现、评审与版本收口记录

1. Phase 3D 按 `docs/phases/PHASE_03D_IMPORT_FINALIZATION.md` 完成八个任务包，实现提交从 `06f2d58` 至 `2098946`，随后补齐不可变镜像部署与 VPS 验收链路。
2. 首轮独立 Evaluator 报告提交 `12716d2`，最终结论 FAIL：HIGH 2 / MEDIUM 5 / LOW 1。MEDIUM-05 为 VPS 的 127 个历史测试批次，其后由用户裁定归入项目完工时生产库归零重置，不阻断复核 PASS。
3. 缺陷修复提交依次为 `bf5baab`、`1b4c06b`、`f13b17d`、`3aa200c`、`5b67393`、`1c32ab4`、`49fc930`、`c87b26f`、`45ee802`；其中 `c87b26f` 是用户授权的 DEC-026 部署工作流纠正，没有重建一次性 bootstrap token。
4. 候选 `45ee802` 通过 CI Run `30688920855`、Container images Run `30689138347`、Deploy Run `30689392268`，并在 `futures` VPS 通过 Phase 3C/3D E2E、schema invariants、RLS、对象持久化和秘密扫描。
5. 独立 Evaluator 在 `docs/reviews/PHASE_03D_EVALUATION.md` 复核章节逐项确认 HIGH-01、HIGH-02、MEDIUM-01 至 MEDIUM-04、LOW-01 CLOSED；复核 docs 提交 `6ff4c2b`，最终 PASS，剩余 BLOCKER/HIGH/MEDIUM/LOW 均为 0。
6. `origin/phase/03-import-foundation` 以普通 merge commit `33aa838c9ef3f39c4e32bb5749982d82d358bf7a` 合入 `main`，没有 squash/rebase。唯一冲突为 `.github/workflows/deploy-futures.yml` 的 DEC-026 add/add 冲突，显式采用 phase 版本；`ci.yml` 自动合并后与 phase 版本一致。
7. merge SHA 的 main CI Run `30703979390` success；标签 `phase-3-pass-20260801` 指向该 merge commit，没有创建 `v*` 标签。标签自动触发的 Container images Run `30704223198` success；未触发 deploy-futures，VPS 未重新部署。

## 已确认的标准发布流程

确认日期：2026-07-26；首次完整生产候选链路实跑完成于 2026-08-01。

- 本地 Git 仓库是唯一源码源头，所有变更先提交再推送 GitHub 私有仓库。
- Codex Cloud / GitHub Actions 承担云端编译测试；GitHub Actions 构建并发布
  `linux/amd64` API、Worker、前端、Collector 镜像到 GHCR。
- `futures` VPS 禁止直接修改源码或手工编译；其 4 GiB 资源允许仓库级 self-hosted
  runner 在 2.5 GiB 总峰值护栏内执行 CI 与镜像构建，部署仍只执行备份、
  `docker pull`、数据库迁移、真实 RLS/持久化和最终 E2E 验收。
- 部署只能引用 SHA 标签或完整 digest，不能只使用 `latest`。
- 生产部署前必须备份数据库；失败时按发布记录中的上一稳定镜像 digest 回滚。
- 生产密钥不得进入 Git、镜像层、构建日志、构建参数或普通环境变量文件。
- GitHub Actions / Codex Cloud 通过不能替代 `futures` VPS 验收。
- GHCR digest 部署已经实际运行：Phase 4A HIGH-03 候选 `e627ab8c3b797cc77f872a9c02439c1dfca0d4eb` 的 Container images Run `30970280360` 发布 API、Worker、Frontend、Collector 四镜像，Deploy Run `30971024520` 按完整 digest 完成备份、迁移、切换与 VPS E2E；当前 VPS 运行该版本。

## 收口后待办与下一步

- MEDIUM-05：原 127 个历史测试批次未清理；Phase 4A 部署前实际手动批次为 144，全部继续如实登记并受指纹保护。按用户裁定在项目完工时执行生产库归零重置；未经单独运维授权不得提前清理。
- TLS：生产 HTTPS 入口与 `AUTH_COOKIE_SECURE=true` 验证按用户裁定延后到项目完工时处理。
- 备份：备份自动化与 master-key 离线副本纳入 Phase 9 完工事项。
- 下一步：Phase 4B-1 按用户指令保持暂停，恢复另单；Phase 4B-2 待另单。Phase 5
  修订版 Planner 契约先推送并等待确认，未确认前不得实现、部署或把 Phase 4B-2
  视为已启动。

## 变更规则

- 新需求先进入产品需求和待确认事项，不直接进入实现。
- 业务口径变化必须同步更新数据库、API、测试样例和验收标准。
- 对原始数据、公式、分类和映射的变更必须保留版本与迁移策略。

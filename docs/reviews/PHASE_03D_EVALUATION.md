# Phase 3D 独立评审报告

评审日期：2026-08-01

评审范围：代码与测试范围 `6dfea78..229b948`；`229b948` 之后的 `e5f262d`、`95bd42d`、`4278396` 仅作为文档和环境实态背景阅读，不计入代码结论。

评审方式：独立 Evaluator 静态审阅、本地门禁复跑、GitHub Actions 只读取证及 `futures` VPS 既有证据与只读聚合查询。

最终结论：**FAIL**

## 1. 最终结论

- BLOCKER：0
- HIGH：2
- MEDIUM：5
- LOW：1
- Phase 3D 的主路径已经落地，CI、不可变镜像部署、Phase 3C/3D E2E、RLS、SSE 重验、回滚 insert/update 主路径、补偿主路径和对象 quarantine 主路径均有通过证据。
- 但补偿上传的幂等/拒绝路径会永久增加未登记对象，存在可重复的磁盘耗尽风险；同时 `soft_delete` 被数据库与领域契约声明为合法 change log 操作，却未被预检和回滚 Worker 支持。两项均违反唯一实施契约，且后一项属于回滚失败，因此本轮不能 PASS。
- 本报告只新增本文件；未修改业务代码、迁移、`PLANS.md`、VPS 源码或数据，未合并 `main`，未创建标签。

## 2. HIGH

### HIGH-01：补偿上传在幂等/可见性判定前提交对象，可被重复请求永久耗尽磁盘

#### 证据

- `rust/apps/api/src/imports.rs:612-776` 的 `create_compensation` 先接收完整文件；`rust/apps/api/src/imports.rs:704-710` 调用 `object_upload.commit()`，明确把对象持久化并说明重放或登记失败时交给对象治理。
- 原批次可见性、允许状态和幂等键检查直到后续 `database::compensations::create_compensation_upload` 才发生；原批次查询见 `rust/crates/database/src/compensations.rs:87-108`，幂等查询见 `rust/crates/database/src/compensations.rs:58-86`。
- 同键同参重放、同键异参 `409`、不可见/不允许原批次均发生在对象已经 commit 之后；这些路径没有把本次新对象登记到 `stored_objects`，也没有复用首个对象。
- `.env.example:17` 的单文件上限是 `52428800` 字节。`admin` 和 `analyst` 均拥有补偿权限，见 `rust/apps/api/src/auth.rs:366-371`。
- Phase 3D 明确禁止物理删除；`ObjectStorage` trait 没有 delete，VPS E2E 也记录 `governance_physical_delete_count=0`。因此 scan/quarantine 只能发现并移动这些对象，不能回收容量。
- 现有测试只验证数据库层补偿幂等和单次 E2E 补偿；仓库中没有“补偿 HTTP 重放前后对象文件数不变”的测试。

#### 复现步骤

仅在隔离测试环境执行：

1. 以有补偿权限的用户准备一个已结束原批次和一个接近 50 MiB 的合法 CSV。
2. 使用同一 `Idempotency-Key`、同一文件和同一 reason 连续调用补偿接口；也可对不存在的批次 ID 重复调用。
3. 首次请求创建一条补偿链；同参重放返回原响应，不存在批次返回不可见错误，同键异参返回稳定冲突。
4. 分别统计对象根中的文件数、`stored_objects`、`import_files` 和 `import_compensations`。除首次请求外，每次请求仍新增一个物理对象，但不会新增对应数据库对象/文件/补偿记录。
5. 执行对象 scan/quarantine 后文件仍占用磁盘，只是被移动到 quarantine。

#### 影响与建议

这是有权限用户可稳定触发的持久存储耗尽路径，也会把正常网络重试转化为不可回收的孤儿对象。应在对象仍为临时上传时完成原批次可见性检查及幂等决议；同参重放应丢弃临时文件并返回首个结果，同键异参/业务拒绝也不得 commit 新对象。修复后必须增加 HTTP 级重放、异参、不可见原批次和并发测试，断言数据库记录数与物理对象数均不增长。

### HIGH-02：合法 `soft_delete` change log 无法预检或回滚

#### 证据

- `rust/migrations/202607260001_phase_3d_rollback_and_object_governance.sql:110-121` 把 `soft_delete` 与 `insert`、`update` 一同定义为合法 `import_row_changes.operation`，且要求 before/after snapshot。
- `rust/crates/domain/src/import.rs:309-323` 公开 `ImportChangeOperation::SoftDelete`。
- `rust/crates/database/src/imports.rs:1940-1953` 的回滚预检只接受 `insert | update`，其他合法值写入 `unsupported_change_operation` 冲突。
- `rust/crates/database/src/rollback_jobs.rs:144-201` 的执行器也只有 `insert` 和 `update` 分支，`soft_delete` 直接返回 `InvalidFrozenImport`。
- 唯一实施契约第 7.2 节要求恢复 `soft_delete` 删除前状态，第 12.1 节要求 insert/update/soft-delete 逆序算法测试；当前 115 项 Rust 测试没有 soft-delete 逆向恢复测试。

#### 复现步骤

仅在隔离数据库执行：

1. 按迁移约束建立一个 `succeeded`、`rollback_capability='direct'`、`change_log_version=1` 的测试批次，并插入连续、来源完整的 `soft_delete` change row。
2. 调用 rollback-check；预检返回 `unsupported_change_operation`，即使该 operation 对数据库和领域模型合法。
3. 若直接构造受控 rollback job 进入 Worker，执行器在 operation match 的默认分支失败，不能恢复 before snapshot。

#### 影响与建议

这是明确的回滚契约缺失。当前正式导入只生成 insert/update，因此未升级为 BLOCKER；但数据库和公共领域类型已经承诺 soft-delete 可回滚，任何合法该类 change log 都会失败。应实现受控 soft-delete snapshot 的校验、锁定和恢复，并增加单元、PostgreSQL、冲突零变更和 VPS E2E 测试。

## 3. MEDIUM

### MEDIUM-01：lineage 递归没有契约要求的深度上限

- 迁移触发器在 `rust/migrations/202607260001_phase_3d_rollback_and_object_governance.sql:695-729` 拒绝自引用和循环，这是有效防线。
- 但 `rust/crates/database/src/compensations.rs:294-339` 的 ancestors/lineage 两个 `WITH RECURSIVE` 都只递增 `depth`，没有 `depth < limit`、数据库 `CYCLE` 条款或 API 配置上限；创建补偿也没有链深上限。
- 契约第 5.4 节明确要求“链路查询必须有深度上限并检测循环”。当前只满足循环检测，合法但很深的用户可控补偿链会导致无界递归结果和随后逐节点 jobs/rollbacks 查询。
- 复现：在隔离环境连续创建多层补偿，再查询任一深层节点 lineage；观察查询工作量随深度持续增长且没有稳定的 depth-limit 错误。

### MEDIUM-02：跨 Workspace 公平轮询已实现，但缺少真实数据库持续负载证明

- `rust/crates/database/src/worker_scheduler.rs:32-101` 使用持久 ticket、稳定排序和 Workspace 行锁，策略本身可审阅且不依赖进程内状态。
- 唯一“持续负载”测试 `sustained_import_load_cannot_starve_another_workspace_or_object_queue` 位于 `rust/crates/database/src/worker_scheduler.rs:172-216`，只对内存中的四个 `Candidate` 循环 400 次，没有调用真实 `reserve_next_work`、PostgreSQL、RLS、`SKIP LOCKED` 或两个 Worker。
- `rust/tests/phase_3d_e2e.sh` 没有公平性/饥饿场景。因而 Phase 3C 遗留的公平轮询 MEDIUM 在代码层已实现，但尚未达到 Phase 3D 契约第 10、12.2 节要求的持续灌入和多 Worker 数据库证明。
- 复现：`rg -n "fair|starv|dispatch_ticket|last_served" rust/tests rust/crates/database/src/worker_scheduler.rs`，可看到生产算法和纯内存测试，但没有数据库/E2E 测试入口。

### MEDIUM-03：对象治理的强制故障注入矩阵不完整

- 对象 scan/check/quarantine 主路径存在，VPS E2E 覆盖 fresh `commit_outcome_unknown`、stale orphan、quarantine 和物理删除计数 0；本地存储测试覆盖路径穿越、符号链接和重复 quarantine。
- 但契约第 8.3、12.5 节要求的 rename 后数据库失败、commit 响应不确定、进程中断、数据库记录缺对象、哈希错误、过期临时文件、重复 scan 的端到端故障注入没有全部落到现有测试。
- `rust/apps/worker/src/object_governance.rs:254-275` 的测试主要通过源码字符串确认分类名称；`rust/tests/phase_3d_e2e.sh:774-816` 只实际制造 fresh/stale orphan。分类代码存在不等于故障恢复被执行验证。
- 因此 Phase 3A“对象孤儿治理”已具备功能闭环，但完整自动化关闭证据不足。
- 复现：列出 `phase_3d_e2e.sh` 中实际创建的对象异常夹具，再与契约第 8.3 节逐项对照；数据库缺对象、哈希错误、过期 `.tmp`、rename 后数据库失败和重复 scan 没有对应的执行断言。

### MEDIUM-04：前端组件测试未覆盖 Phase 3D 规定的完整交互矩阵

- Phase 3C 的历史项已有直接关闭证据：`frontend/src/components/ImportWorkflowPanels.test.ts:12-109` 覆盖确认/回滚二次确认和补偿面板，`frontend/src/importProgress.test.ts:46-103` 覆盖断线重连与撤权停止。
- 但契约第 9、12.4 节还要求重复提交、冲突游标分页、陈旧预检、错误终态、补偿链接和失败恢复的组件级交互；当前 Vitest 只有 5 files / 13 tests，没有挂载完整 `ImportCenterView` 的上述状态机测试。
- `frontend/src/api.test.ts` 的陈旧/冲突测试只验证 API client 保留结构化错误，不验证页面清理旧预检、分页追加、按钮禁用或失败恢复。
- 这不重新打开 Phase 3C 的“确认面板/断线重连”历史项，但 Phase 3D 自身的完整前端退出矩阵尚未满足。
- 复现：运行 `pnpm test` 并列出 13 个测试名，再搜索 `mount(ImportCenterView`、rollback conflict cursor 翻页、stale precheck 页面恢复和失败终态交互；均没有组件级用例。

### MEDIUM-05：VPS 生产库实态与交接中的 `import_batches=0` 不一致

- 2026-08-01 的只读管理员聚合查询得到：`users=31`、`import_batches=127`、`import_files=127`、`stored_objects=127`、`imported_records=732`、`import_job_events=406`。
- 127 个批次全部关联已知测试命名模式的 Workspace owner，创建时间集中在 2026-07-25；状态分布为 uploaded 5、preview_ready 25、succeeded 90、failed 7。没有活动 Session，也没有 Phase 3D E2E 临时 trigger。
- 这说明没有真实用户业务批次，但“批次为 0”的给定事实不成立。最终 Phase 3D E2E 自己创建的 UUID scope 已清理；残留来自更早的测试运行。
- 复现只读查询：以数据库管理员执行 `select count(*) from import_batches`，并按 status、created_at、Workspace owner 的测试模式做聚合；不得输出用户名或删除记录。
- 该偏差不否定最终脚本的 scope 清理断言，但在真实数据进场前仍需按单独运维授权完成测试库重置，且后续交接不得用无 Workspace 上下文的 RLS 查询把管理员实际行数误记为 0。

## 4. LOW

### LOW-01：代码审查范围的 `git diff --check` 门禁返回非零

- 复现：`git diff --check 6dfea78..229b948` 返回 exit 2。
- 命中 `docs/reviews/PHASE_03B_EVALUATION.md:3-5` 和 `docs/reviews/PHASE_03C_EVALUATION.md:3-5` 共 6 处行尾双空格，均是 Markdown 强制换行写法，不影响运行时。
- 因唯一契约第 13.1 节明确列出 `git diff --check`，该项应如实记为 FAIL；本单只读，没有修改历史评审文件。

## 5. Phase 3D 退出门禁逐项核验

| 门禁 | 结论 | 证据 |
| --- | --- | --- |
| 正式导入 change log 与能力标记同事务 | PASS | `execute_import_job` 在 `rust/crates/database/src/job_queue.rs:400-894` 同一事务写正式记录、`import_row_changes`、direct/version、job/batch 终态、事件和审计；测试 `changes_capability_terminal_state_event_and_audit_share_one_commit`、`change_sequence_is_contiguous_and_only_advances_when_requested` 通过。 |
| rollback-check 全量预检与幂等入队 | PASS | `create_rollback_check`、`queue_rollback` 位于 `rust/crates/database/src/imports.rs:1338-1598`；VPS E2E 覆盖 105 目标、210 冲突、三页完整分页、20 路同键并发仅一个 job、同键异参稳定 409。 |
| Worker 单事务原子回滚 | FAIL | insert/update 主路径与 generation fence 通过，VPS 成功逆序恢复 2 条并同事务落失效/状态/事件/审计；但 HIGH-02 证明合法 soft-delete 无法执行。 |
| 任一冲突整体中止、正式数据零变更 | PASS | VPS E2E 对 105 条后续修改保存前后摘要并验证相等；陈旧 fingerprint 和补偿依赖场景也验证业务摘要不变。`rollback_has_only_atomic_success_or_conflict_commits` 通过。 |
| 补偿与 lineage | FAIL | 单次补偿完整 upload→inspect→mapping→preview→validate→confirm 与两节点 lineage 在 VPS 通过；但 HIGH-01 的重放/拒绝对象泄漏和 MEDIUM-01 的无深度上限违反契约。 |
| 旧批次禁止伪造 backfill | PASS | 迁移默认并更新为 `compensation_only`/null；VPS 实态 127/127 批次均为 `compensation_only:null`，`import_row_changes=0`，没有伪造 change rows。 |
| 对象 scan/check/quarantine/audit 且无物理删除 | FAIL | 功能主路径、10 类分类代码、VPS orphan/quarantine/audit 与 `governance_physical_delete_count=0` 通过；但 MEDIUM-03 的强制故障注入矩阵不完整。 |
| SSE OpenAPI 帧 schema、重放和周期性重验 | PASS | `ImportSseEventFrame` discriminated schema 与 OpenAPI 快照测试通过；`Last-Event-ID` 前端/API 测试通过；VPS 撤销 Session 后 SSE 终止并写 `import.events_access_terminated` 审计。 |
| RLS 与跨 Workspace 拒绝 | PASS | Phase 3D 10 张新表均 `ENABLE/FORCE RLS`，VPS 查询为 `phase3d_rls=10,forced=10,policies=10`；`futures_runtime` 为 LOGIN、非 superuser、无 BYPASSRLS；VPS E2E 的跨 Workspace API 404、SELECT 0 行、INSERT 拒绝通过。 |
| 审计与秘密扫描 | PASS | VPS E2E 核对 9 类关键审计、审计 metadata 禁止 key 0 命中；部署报告 `service_log_secret_scan=PASS`。本地对审查差异扫描 GitHub token、私钥头、Bearer、带密码 PostgreSQL URL 均 0 命中，敏感文件名 0。 |
| 本地与 CI | FAIL | 用户指定的 Rust fmt/clippy/test 与 pnpm lint/test/build 全部通过；但 LOW-01 的 `git diff --check` 失败，本机无 Docker。CI Run `30209113534` 的 validate 和三个镜像 build job 全部 success，含 Compose config。 |
| VPS 最终验收 | PASS | Deploy Run `30209326834` success；保留证据各含唯一 `PHASE3C_E2E_PASS`、`PHASE3D_E2E_PASS` 和 `PHASE3D_SCHEMA_INVARIANTS_PASS`。运行版本、三镜像 digest、迁移、健康和证据哈希相互一致。 |
| 六项历史 MEDIUM 全部关闭 | FAIL | 见第 6 节：4 项有完整关闭证据，公平轮询和对象孤儿治理的自动化证据仍不完整。 |
| 明确排除项未实现 | PASS | 见第 7 节。 |

## 6. 六项历史 MEDIUM 关闭核验

| 来源 | 历史项 | 结论 | 关闭证据或缺口 |
| --- | --- | --- | --- |
| Phase 3A | 对象孤儿治理 | 未完全关闭 | scan/check/quarantine/audit 和无物理删除已实现并在 VPS 通过；但 MEDIUM-03 所列故障注入没有全部执行验证。 |
| Phase 3A | 可重复 API/DB/RLS 自动化 | PASS | `phase_3c_e2e.sh`、`phase_3d_e2e.sh`、`phase_3d_schema_invariants.sql` 由部署工作流自动运行；本地脚本 SHA 与 VPS 证据一致。 |
| Phase 3C | SSE OpenAPI 帧 schema | PASS | OpenAPI `text/event-stream` 直接引用 discriminated `ImportSseEventFrame`，稳定错误 400/401/403/404/500 有快照测试。 |
| Phase 3C | 跨 Workspace 公平轮询 | 未完全关闭 | 持久 ticket 策略已实现；缺少真实 PostgreSQL、持续灌入和多 Worker 的不饥饿测试，见 MEDIUM-02。 |
| Phase 3C | 长连接周期性重验会话 | PASS | 间隔可配置且有上限；服务端重验 Session、用户、Workspace、权限、批次可见性；VPS 撤权终止与审计通过。 |
| Phase 3C | 确认面板/断线重连组件级测试 | PASS | `ImportWorkflowPanels.test.ts` 覆盖确认面板，`importProgress.test.ts` 覆盖断线携最后事件 ID 重连与撤权停止。Phase 3D 更广的前端矩阵缺口另见 MEDIUM-04。 |

## 7. 越界核验

结论：PASS。

- 路由中没有 cancel、人工 dead-letter replay 或冲突候选人工合并入口。
- `dead_letter` 仅保留既有 Worker 自动终态和展示；没有人工 replay API/按钮。
- `cancelled` 是 Phase 3A 已存在的领域状态，没有新增 cancel 路由或执行语义。
- 代码差异没有新增行情/套利/交易/持仓/席位/图表、外部采集、浏览器/noVNC、OCR、AI 或回测模块、表、路由或 UI。
- 导入数据集中的 `trade_date` 是既有 generic import 字段，不构成交易业务模块。

## 8. 本地回归实际输出摘要

| 命令 | 结果 | 实际摘要 |
| --- | --- | --- |
| `cargo +stable fmt --check` | PASS | exit 0，无输出。 |
| `cargo +stable clippy --workspace --all-targets -- -D warnings` | PASS | 7 个 workspace crate 检查完成，0 warning，约 3.8 秒。 |
| `cargo +stable test --workspace` | PASS | 115 passed、0 failed；分组为 19、12、60、12、7、5，doc-tests 均通过。 |
| `pnpm lint` | PASS | `vue-tsc --noEmit` exit 0。 |
| `pnpm test` | PASS | 宿主权限复跑为 5 files / 13 tests passed。首次沙箱运行因 Vite/esbuild 无权读取工作区父目录而在启动阶段失败，不是断言失败。 |
| `pnpm build` | PASS | 宿主权限复跑转换 1468 modules，构建约 3.5 秒；仅有现存的 >500 kB chunk warning。首次沙箱运行同样因 esbuild 目录权限失败。 |
| `git diff --check 6dfea78..229b948` | FAIL | exit 2，6 处历史评审 Markdown 行尾双空格，见 LOW-01。 |
| `docker compose config` | 本机不可执行 | 本机没有 Docker CLI；CI Run `30209113534` 的 Docker Compose config step success，VPS 部署脚本也在切换前执行 config。 |

## 9. CI、部署与 VPS 证据

### GitHub Actions

- CI Run `30209113534`：事件 `push`，分支 `phase/03-import-foundation`，HEAD `229b948bfcbdfcbe683aac816967e50fcf6baa90`，结论 success。
- CI jobs：`validate`、`Build api image`、`Build worker image`、`Build frontend image` 全部 success；validate 的 Rust fmt/clippy/test、pnpm install/lint/test/build、Compose config 各 step 均 success。
- Deploy Run `30209326834`：`workflow_dispatch`，工作流 HEAD 同为 `229b948`，部署 job 全部 step success。
- 部署候选 `d15fe49841e5a66040c37d57d5943f9e431800fa` 是 `229b948` 的祖先；`d15fe49..229b948` 只新增部署 workflow 并修订 Phase 3D 验收脚本，没有业务代码差异。工作流通过 `GITHUB_SHA` 使用当次最新 Phase 3D harness 验收候选。

### VPS

- `/api/v1/version` 返回 `git_sha=d15fe49841e5a66040c37d57d5943f9e431800fa`；ready 为 HTTP 200，API/PostgreSQL healthy，五个容器均运行。
- API digest：`sha256:f9269ad47e11f6eb7b279c7fa8f3076323ecff88900d7ecd540637682aeb5cea`。
- Worker digest：`sha256:ba8791208d6cd9f89fac2ca9e53feb3dfe48f647f916a93c9884bcdb828b9034`。
- Frontend digest：`sha256:49ea13f01de824194f7a4e95ac2bbb0125220eb05c41831ea6b1bb416e396028`。
- 迁移 `202607260001`、`202607260002` 均在 `schema_versions` 中。
- 本地 `phase_3d_e2e.sh` SHA-256 为 `02134e1ed65b1e16f1421bbccd0089f812f292de0139d1e8cde76c651d8de48c`，与 VPS stdout 记录一致。
- VPS 保留证据中 Phase 3C/3D PASS marker 各恰好 1 个；Phase 3D stdout SHA-256 为 `9bc2099808046044d2c42a3847da9c694b11a4d728f6017400578ccfd3e291d8`。
- bootstrap-token 宿主文件当前不存在；这是 2026-08-01 已登记并人工闭环的 DEC-026 运维事实，不作为本报告异常计数。`auth.rs` 对只读挂载删除失败仍静默吞错，代码层修复或明确宿主删除标准流程应归入后续普通改进。

## 10. Git 基线、分叉与后续 merge 影响

- `origin/phase/03-import-foundation` 当前为 `4278396`；代码审查截止 `229b948` 后只有三个文档提交，且 `git diff 229b948..HEAD -- rust frontend .github deploy docker-compose*` 无业务/工作流差异。
- `origin/main` 当前为 `78f0673`，两分支 merge base 是 Phase 2 收口 `6dfea78`。
- `main` 独立包含 8 个 CI/部署提交 `feee231..78f0673`；phase 分支的对应改动分布在 `1e353ff`、`b81fde6..5983c10` 和 `6b6edb3`，hash 均不同，并额外包含 `49e2018` 等验收脚本提交。
- 当前两分支的 `container-images.yml` 和 `deploy-futures.yml` 内容一致；`.github/workflows/ci.yml` 不一致，phase 分支包含固定 action SHA、Rust cache、生产 Compose config 和三镜像 CI build job。
- 只读 `git merge-tree` 预检未产生 conflict marker。普通 merge 预计保留两条 CI 历史并采用 phase 的较完整 `ci.yml`；虽然内容冲突风险低，合并后仍必须在 `main` 重新运行 CI，不能把 phase Run `30209113534` 当作 main 合并提交证据。
- 本轮按用户约束不执行 merge、不打标签；由于结论 FAIL，也不满足 Phase 3 PASS 合并/标签前置条件。

## 11. 最终判定

**FAIL**

- BLOCKER：0
- HIGH：2
- 需要 Generator 修复 HIGH-01、HIGH-02，并补充相应 API/对象计数、soft-delete PostgreSQL/Worker/VPS 回归；随后由独立 Evaluator 复核。
- MEDIUM-01 至 MEDIUM-05 也应在最终 PASS 前给出关闭证据或由用户明确重新裁定范围；不得以既有 CI/VPS PASS 覆盖本报告发现。

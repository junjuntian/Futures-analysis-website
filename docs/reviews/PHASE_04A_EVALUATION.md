# Phase 4A 独立评审报告

评审日期：2026-08-03

评审范围：代码范围固定为 `main@2a6e6d557b327a33ff2f6c5e694ab19c1dddebac..944a4defe578d5922b9f1ea83f951ddbd6fb005e`；其后的 `1a27e072c1d5cb7dbcb6ec4afe414363beaf2bc9` 仅作为文档提交核对，不计入代码结论。

评审契约：`docs/phases/PHASE_04_AKSHARE_COLLECTION.md`、DEC-031、DEC-038、DEC-039、DEC-041；评审流程遵循 `.agents/Evaluator.md` 与 DEC-036。

评审方式：全新独立 Evaluator 静态审查、提交级差异审查、本地门禁复跑、GitHub Actions 只读取证、`futures` VPS 只读实证与数据库聚合查询。`HANDOFF_20260803_0008.md` 仅用于定位证据，不采信其结论、计数或 PASS 声明。

最终结论：**FAIL**

## 1. 最终结论与缺陷计数

- BLOCKER：0
- HIGH：5
- MEDIUM：4
- LOW：1
- Rust、前端、Python、ruff 与 `git diff --check` 本地门禁均通过；指定的 CI、镜像和部署 Run 均为 success，VPS 正在运行候选版本 `944a4de`，并保留唯一 `PHASE4A_E2E_PASS`。
- 五所采集、DCE 官方优先后回退、来源标注、一次性容器、数据库凭据隔离、RLS、唯一键、批次/来源/变更链、cron 与 512 MiB 上限的主路径已有有效证据。
- 但出站请求策略会在重定向实际完成后才校验目标，不能满足逐跳校验及 DNS rebinding 防护；DCE 从聚合源恢复官方源时会被来源无关的导入业务键阻断；Phase 4A 正式投影的外键依赖未进入 rollback-check；DCE fallback 可以吞掉单合约错误并把不完整数据集记为成功；服务账号与通用手动入口也没有双向隔离。上述均为契约主路径或安全/数据完整性缺陷，故不能 PASS。
- 本报告是本单唯一新增文件；未修改业务代码、未合并 `main`、未打标签、未启动 Phase 4B。

## 2. HIGH

### HIGH-01：出站白名单在重定向完成后才校验，无法阻止被禁止目标已被访问

#### 证据

- `collector/src/futures_collector/sources.py:316-326` 在请求前解析白名单域名并拒绝非公网解析结果；这能阻止直接传入私网、metadata 或非白名单 URL。
- `collector/src/futures_collector/sources.py:329-349` 替换 `requests.sessions.Session.request`，但在 `original(session, method, url, ...)` 返回后才检查 `response.url`。`requests` 的重定向跟随发生在这个 `original` 调用内部，因此中间跳和最终跳已经建立连接并读取响应后，校验才发生。
- 同一实现先独立调用 `socket.getaddrinfo` 校验，再由连接栈重新解析并连接；它没有把已验证 IP 固定到实际连接，也没有逐次连接前复验，不能兑现契约要求的 DNS rebinding 拒绝。
- `collector/tests` 只证明直接的非白名单主机会被拒绝；没有白名单域名 `302` 到私网/metadata/非白名单公网、连续多跳、或解析结果在校验和连接之间变化的反向用例。

#### 复现步骤

仅在隔离测试网络执行：

1. 令一个白名单测试域名返回 `302 Location: http://127.0.0.1:<port>/probe`，本地 probe 记录是否收到请求。
2. 在 `official_requests_only` 上下文内通过 `requests.get` 请求白名单入口。
3. 当前代码最终会抛出 `OutboundPolicyError`，但 probe 已收到请求；把目标换为第二个非白名单公网域名也同样先访问后拒绝。
4. 再使用可控制 DNS 的测试域名，在 `_validate_public_host` 查询时返回公网地址、连接查询时返回私网地址；当前校验和连接没有绑定，无法稳定拒绝该竞态。

#### 影响与建议

这是有真实 SSRF/内网探测影响的安全边界缺陷。应关闭库的自动重定向，逐跳以 `allow_redirects=false` 请求、在每一跳连接前验证 scheme/精确域名/解析地址并限制跳数；实际连接必须绑定或复核已验证地址，拒绝私网、loopback、link-local、保留地址和云 metadata 地址。增加能够证明“被禁止目标完全未收到请求”的集成测试，而不是只断言最终抛错。

### HIGH-02：DCE fallback 数据的来源无关导入键阻断恢复官方源

#### 证据

- DEC-041 要求 DCE 官方源优先，并在 AKShare 官方入口恢复后自动回到官方源；正式行情/席位唯一键也包含 `source_id`，见 `rust/migrations/202608020001_phase_4a_collection_schema.sql:182-195,239-252`。
- 但 `rust/crates/application/src/import_jobs.rs:371-443` 生成行情和席位 `business_key` 时不包含 `source_id` 或 `data_source_code`。当前 DCE fallback 与未来 DCE official 会得到同一导入业务键。
- `rust/crates/database/src/job_queue.rs:1053-1068` 在投影前按 `(workspace_id,dataset_type,business_key)` 锁定旧 `imported_records`：数据相同则直接 skip，数据不同则返回 `SourceRevisionConflict`。两条分支都不会创建官方来源的正式事实。
- VPS 当前 DCE 行情 200 行和席位 5340 行均来自 `akshare_sina_dce_fallback`；官方 DCE 的 catalog/market/seats/calendar 提取均有 failed 审计，fallback 四类数据均有 succeeded 审计。因此官方源恢复不是抽象情形，而是下一次正常源切换必须经过的路径。
- 正式表来源列和 `data_sources` 白名单本身正确；缺陷发生在到达正式投影之前，不能由正式表含 `source_id` 的唯一键补救。

#### 复现步骤

1. 在隔离数据库先按当前现网方式导入某日 DCE fallback 行情或席位。
2. 使用同一交易日、同一合约/席位/排名构造 `akshare_dce_official` 自动批次。
3. 若数值完全相同，Worker 在 `imported_records` 层 skip，正式事实仍标注 fallback；若数值有差异，Worker 以 `SourceRevisionConflict` 失败。
4. 查询正式表按 `source_id` 分组，无法得到契约要求的官方源事实，也没有实现自动回归官方源。

#### 影响与建议

该缺陷会把临时例外来源固化为长期来源，直接违背 DEC-041 的恢复边界。自动事实数据的幂等键必须与正式业务身份一致并包含受控来源身份；同时明确同日 fallback 与后到 official 的优先、替换或版本策略，并用“fallback 成功后官方恢复”的 PostgreSQL、Worker 和 VPS 回归覆盖相同值与不同值两种情形。

### HIGH-03：rollback-check 未检查 Phase 4A 正式投影依赖，会把不可回滚批次报告为可直接回滚

#### 证据

- `rust/crates/database/src/imports.rs:2200-2450` 的预检遍历 `import_row_changes`，只允许 `target_kind='imported_record'`，随后只锁定 `imported_records`、搜索后续 `import_row_changes` 和 `import_conflict_candidates`。没有查询 `exchanges`、`instruments`、`contracts`、`trading_calendar_versions`、`market_prices` 或 `seat_positions`。
- `rust/crates/database/src/rollback_jobs.rs:125-166` 执行 insert 回滚时直接删除 `imported_records`。迁移把目录/日历投影的 `source_record_id` 设为 `ON DELETE CASCADE`，但行情与席位对 `contracts`、行情对 `trading_calendar_versions` 的外键是 `ON DELETE RESTRICT`，见迁移 `:90-195,223-252`。
- 因而目录或日历批次在被后续事实引用后，预检仍可能返回 `can_rollback=true`；真正 Worker 删除时才触发外键错误，用户看到的是“预检允许、执行失败”，而不是契约要求的完整依赖冲突清单与零副作用拒绝。
- VPS 只读聚合证实：有 5 个 `succeeded/direct/change_log_version=1` 的目录批次，其合约已被行情或席位引用；另有 5 个同类日历批次，其版本已被行情引用。现有生产数据已经落入该场景。
- `rust/tests/phase_4a_e2e.sh` 没有调用 rollback-check，也没有执行 Phase 4A 批次的成功回滚或依赖冲突回滚；`PHASE4A_E2E_PASS` 不能覆盖此缺陷。

#### 复现步骤

1. 在隔离环境导入一个目录批次和日历批次，再导入引用其 contract/calendar version 的行情或席位批次。
2. 对目录或日历批次调用 rollback-check。
3. 当前预检只看到其 `imported_record` 快照与旧冲突表，可返回 direct/can_rollback。
4. 入队执行后，删除目录或日历 `imported_record` 触发 cascade，再被事实表的 restrict 外键阻止；批次进入回滚失败，而预检没有返回具体依赖。

#### 影响与建议

这破坏了“正式表落库具备回滚链”和回滚预检可信度。应让 Phase 4A 投影成为 change log 的受控 target，或在预检中显式锁定并枚举所有投影和下游外键依赖；Worker 必须只执行预检已经证明安全的逆序动作。修复后增加目录、日历、行情、席位四类批次的成功回滚、下游依赖冲突、陈旧预检和原子零变更 E2E。

### HIGH-04：DCE fallback 吞掉单合约错误，可能把不完整“全市场”数据记为成功

#### 证据

- `collector/src/futures_collector/sources.py:130-162` 对 fallback 行情逐合约调用 Sina；除 `OutboundPolicyError` 外的异常只写 `dce_fallback_market_contract_skipped` 并继续。只要至少一个合约成功，函数返回拼接结果。
- `collector/src/futures_collector/sources.py:164-203` 的席位路径对每个合约、每类排名采用相同 continue 策略；某合约或某排名类型缺失不会令数据集失败，只要最终 `tables` 非空即可。
- `collector/src/futures_collector/runner.py:120-149` 收到非空 rows 后会上传并记录 `batch_succeeded`，没有将 skip 数或缺失合约写入批次状态、正式审计元数据或验收断言。
- 契约要求 DCE fallback 仍覆盖 DCE 全市场，且来源/解析错误应使该交易所数据集失败；当前逻辑把部分成功误当完整成功。
- 本次 VPS 已验收运行的日志聚合中 market、seats 的 contract/instrument/detail skip 均为 0，因此现存 200/5340 行没有观察到该问题；这只能证明该次运行完整，不能证明故障分支正确。

#### 复现步骤

1. 在 collector 测试中让 DCE catalog 返回至少两个合约。
2. 让第一个合约返回有效行情/席位，让第二个合约抛普通网络或解析异常。
3. 当前 `fallback_market`/`fallback_seats` 返回非空部分结果，Runner 上传后批次为 succeeded，进程也可返回 0。
4. 对比 catalog 合约集与事实合约集，可看到缺口，却没有失败批次或结构化缺失记录。

#### 影响与建议

不完整行情会以“成功、可重放”的正式数据进入下游分析，属于数据完整性主路径缺陷。应对预期合约集和排名类型做完整性闭合；任一必要请求/解析失败应令 DCE 对应数据集 failed，或按契约重新裁定并引入明确的 partial 状态、缺失清单和告警，不得继续标记 succeeded。

### HIGH-05：服务账号与通用手动入口未双向隔离，可绕过人工确认或固定映射

#### 证据

- `rust/apps/api/src/auth.rs:136-202,447-448,487-500` 把 collector 建成精确用户名、单一 `analyst` 角色；`analyst` 仍拥有通用 Upload、Rollback、Compensate 权限，没有仅供 collector 的最小权限角色。
- `rust/apps/api/src/imports.rs:462-486` 只在请求带 automatic metadata 时要求 collector 身份。collector 省略这些 header 即可创建 `ingestion_mode=manual,dataset_type=generic` 的普通批次。
- `rust/apps/api/src/imports.rs:1401-1445` 的通用 `/confirm` 只检查批次是 manual，没有拒绝 `context.is_collector_account()`；因此服务账号可自行完成本应由人触发的 manual confirm。
- 反方向也未隔离：`inspect`、`save_mapping`、`preview`、`validate` 位于 `rust/apps/api/src/imports.rs:1095-1355`，只要求通用 import write 权限和同 Workspace，没有拒绝 automatic 批次或要求 collector 身份。
- `rust/crates/database/src/imports.rs:970-1061` 的 `save_mapping` 不检查 `ingestion_mode`，允许客户端为 automatic 批次保存同 dataset 的自定义字段映射；它还会直接更新 batch 的 `dataset_type`。
- `rust/apps/api/src/imports.rs:1526-1688` 只有批次仍为 `Uploaded` 时才执行服务端固定 inspect/mapping/full validation。若普通 analyst 已把 automatic 批次推进到 `PreviewReady`，automatic-confirm 接受该状态并跳过固定流水线，随后以 automatic scope 入队。于是“入口不接受客户端 mapping JSON”的契约可以被通用端点旁路。

#### 复现步骤

仅在隔离环境执行两组反向用例：

1. 使用 collector 服务账号登录，上传 CSV 时不带 automatic metadata；依次调用通用 inspect/mapping/preview/validate/confirm。当前身份是 analyst，manual confirm 没有 collector deny，批次可以不经人类账号确认。
2. 使用普通 analyst 找到同 Workspace 内一个 `Uploaded` automatic 批次，调用 inspect、为同一 dataset 保存客户端 mapping、preview 和 validate，使其到 `PreviewReady`。
3. 再让正常 collector 调用 automatic-confirm；当前实现跳过 `Uploaded` 分支的固定映射和全量解析，确认 analyst 预置的映射。
4. 通过交换两个仍能通过类型校验的 CSV 列，比较正式事实与原 CSV 固定字段语义，可证明客户端 mapping 实际影响自动正式数据。

#### 影响与建议

这既允许持有服务凭据者绕过“手动导入仍需人工确认”，也允许普通 analyst 借 automatic 批次写入本应只由采集固定模板产生的正式 Phase 4A 数据。应建立 collector 最小权限身份或在所有通用导入端点双向拒绝不匹配 mode：collector 不得创建/确认 manual 批次，普通用户不得推进 automatic 批次；automatic-confirm 必须无条件校验固定 mapping、验证版本和 actor，不能因已有状态跳过可信流水线。补充完整身份 × ingestion mode × endpoint 的 HTTP/DB 拒绝矩阵。

## 3. MEDIUM

### MEDIUM-01：cron 调用方没有计算“最近交易日”，节假日后的定时任务会传入非交易日

#### 证据与复现

- collector CLI 的 `--date` 是必填参数，没有内部回退，符合契约；见 `collector/src/futures_collector/cli.py`。
- 但 `deploy/collector/run-collector.sh:17-31` 在 cron 不传参时使用上海时区当天日历日期，再显式传给容器。`/etc/cron.d/futures-collector` 的两条命令都没有日期参数。
- 在工作日法定休市日运行 `run-collector.sh`，它会传当天而不是最近交易日；数据源可返回空或失败，不会采集最近一个真实交易日。
- 建议由宿主调度层依据受控交易日历计算最近交易日并显式传入，保留 CLI 不回退；加入节假日和周末边界测试。

### MEDIUM-02：`observed_at` 是固定的 13:30Z，不是实际采集 UTC 时间

#### 证据与复现

- `collector/src/futures_collector/normalize.py:132-163` 把所有行情 `observed_at` 固定为 `<collection_date>T13:30:00Z`。
- 17:30 Asia/Shanghai 的定时任务约为 09:30Z，固定值在该次采集发生时仍位于未来；本次验收采集也不是恰好在 13:30Z 发起。
- 契约定义该字段为采集 UTC 时间。固定时间削弱溯源、延迟分析与批次审计，且两次日内采集无法区分。
- 建议在单次采集开始时取得一次 UTC timestamp，并在该批数据中一致传播；补充断言其位于批次开始/结束时间窗内。

### MEDIUM-03：目录投影只做 `DO NOTHING`，不能按契约重复补全或刷新同一目录实体

#### 证据与复现

- `rust/crates/database/src/job_queue.rs:1291-1357` 对 exchange、instrument、contract 的冲突策略均为 `ON CONFLICT DO NOTHING`，不会更新名称、乘数、tick、上市日或到期日，也不会生成投影层 before/after 变更。
- 同时 `rust/crates/database/src/job_queue.rs:1053-1068` 对同一目录业务键但不同 `record_data` 返回 `SourceRevisionConflict`，因此后续官方目录补齐首批缺少的 `contract_multiplier`、`price_tick` 或日期时，既不能 upsert，也不能记录刷新。
- 复现：首批导入某合约的空乘数/tick，第二批以同一 exchange/instrument/contract 提供数值；当前第二批失败或跳过，正式 contract/instrument 仍为空。
- 契约要求先 exchange、再 instrument、再 contract，并允许重复运行填充/更新，且变更应进入回滚链。建议实现受控 upsert、row_version 与完整 before/after change log。

### MEDIUM-04：E2E 仍有宽松来源匹配，且未覆盖契约要求的自动确认/回滚闭环

#### 证据与复现

- 已修复的四处缺陷不再是恒真断言：基线使用 `>=127` 并比较完整 manual fingerprint；`run_collector_with_peak` 在 `wait` 后返回真实退出码；内存要求 `>0` 且 `<=512 MiB`；用户 fingerprint 排除仅允许变化的会话型字段但保留稳定身份字段。
- 反向验证实际结果：强制 collector 状态为 1 时得到 `reverse_exit_status=EXPECTED_FAIL observed=1`；把 peak 设为 0 得到 `reverse_zero_memory=EXPECTED_FAIL`；令前后稳定 fingerprint 不同得到 `reverse_fingerprint=EXPECTED_FAIL`。三者都证明断言会在应失败条件下失败。
- 但 `rust/tests/phase_4a_e2e.sh:140-142` 的故障隔离只断言“非 DCE official 的成功 source code 数量等于 4”，没有断言精确集合为 SHFE/CZCE/GFEX/CFFEX。若一个应成功官方交易所缺失而 DCE fallback 意外成功，计数仍可能为 4。
- `rust/tests/phase_4a_e2e.sh:90-187` 没有验证普通 analyst/collector 在 automatic/manual 两类批次上的双向拒绝，也没有断言 automatic 批次不存在客户端 preview/mapping 旁路；同时没有调用 rollback-check 或执行 Phase 4A 回滚。HIGH-03 与 HIGH-05 证明这两组遗漏都已漏过真实缺陷。
- 建议用精确 expected-set 差集断言每所/每来源/每数据集，增加普通 analyst、collector 身份、manual/automatic 交叉拒绝和四类正式投影回滚的 API/DB E2E。

## 4. LOW

### LOW-01：一次性容器缺少契约指定的 `/work` tmpfs

#### 证据与复现

- `docker-compose.yml:82-106` 以及 VPS 生效配置只有 `/tmp:size=64m,mode=1777`，没有 `/work`；容器仍是 read-only、无数据库网络/依赖、仅 edge 网络、512 MiB、`cap_drop: ALL`，因此当前代码使用内存 CSV 时没有观察到持久化落盘。
- 运行生效 compose config 并检查 `tmpfs`，只得到 `/tmp`。这与工程契约明确列出的 `/work` 临时工作目录不一致。
- 建议增加限额 `/work` tmpfs，并明确 collector 临时文件根目录；即使当前实现不写临时 CSV，也应让未来库行为保持在受控可写面。

## 5. 契约逐条核验

| 契约项 | 结论 | 证据 |
| --- | --- | --- |
| 一次性容器、版本锁定 | PASS | collector profile、`restart: no`、`run --rm --no-deps`；基础镜像 `python:3.13.11-slim-bookworm`，AKShare 与 Python 依赖均精确锁定；镜像 Run 发布不可变 digest。 |
| `--date` 显式传参、不在 collector 内自动回退 | PASS | CLI 参数 required；Runner/normalize 不寻找相邻日期。调度层“最近交易日”问题另见 MEDIUM-01。 |
| 五所隔离采集 | PASS | 顶层逐 exchange 捕获并记录失败后继续；故障注入 DCE 时进程非零、其余四所成功。故障断言的精确集合缺口见 MEDIUM-04。 |
| CSV → 服务账号 → 导入 API | PASS | collector 只生成内存 CSV，经登录、上传、automatic-confirm API；没有直连 PostgreSQL。 |
| collector 不持数据库凭据 | PASS | 镜像/compose 无 DB 环境和 backend 网络，仅挂载 collector 凭据；凭据 JSON schema 只有 API URL/origin/username/password。 |
| 正式表来源/批次/行号/变更/回滚链 | FAIL | facts 的来源、batch、row、record lineage 和 change rows 完整；但正式投影依赖未进入预检，见 HIGH-03。 |
| 目录建档顺序 | PASS | Worker 顺序解析 exchange → instrument → contract，行情/席位未知合约进入 conflict candidate，不凭空建合约。 |
| 目录重复补全/刷新 | FAIL | 冲突只 `DO NOTHING`，不同数据报 source revision conflict；见 MEDIUM-03。 |
| 幂等唯一键与重放 | PARTIAL | 数据库唯一约束和同源同输入重放计数稳定；跨来源恢复被导入键阻断，见 HIGH-02。 |
| cron、锁、时区 | PARTIAL | cron 文件 root:root 0600，上海时区工作日 17:30/21:30，两条命令均经 `flock`；最近交易日计算缺失，见 MEDIUM-01。 |
| 512 MiB 与只读运行面 | PASS | VPS 生效 mem limit `536870912`，峰值 `130641920`；read-only、pids 128、no-new-privileges、cap drop all 后仅加 SETUID/SETGID。`/work` 偏差见 LOW-01。 |
| 出站白名单与 DNS/redirect 防护 | FAIL | 初始精确域名和公网地址校验存在，但逐跳和 rebinding 防护不成立；见 HIGH-01。 |
| DCE-only fallback、官方优先、来源准确 | PARTIAL | 代码只有 `_require_dce` 能进入 Sina；每次先 official，失败后才 fallback；四所无聚合源；事实准确标注 Sina。官方恢复和部分成功问题见 HIGH-02/HIGH-04。 |
| 服务账号自动确认授权边界 | FAIL | automatic endpoint 的正向校验存在，但通用手动入口可双向旁路，见 HIGH-05 与第 6 节。 |
| RLS 覆盖新表 | PASS | 两个迁移已应用；VPS 11/11 新 workspace 表均 ENABLE/FORCE RLS 且有 policy/grant，runtime 零 workspace 上下文查询为 0。 |

## 6. `a787150` 自动确认授权边界

结论：**FAIL。automatic-confirm 本身的正向身份/数据集校验成立，但 manual 与 automatic 两套入口没有双向隔离。**

- `rust/apps/api/src/auth.rs:30,136-202,447-448` 将身份固定为 username `collector-service` 且角色精确等于单一 `analyst`；provision 要求恰好一个 enabled admin-owned workspace，不授予 admin 角色。
- `rust/apps/api/src/imports.rs:462-486` 只有该精确身份能在上传时声明 automatic metadata；元数据继续受四个固定 dataset、`dataset@1`、日期和 `automatic_source` 精确白名单约束。
- `rust/apps/api/src/imports.rs:1495-1520` 在 `/automatic-confirm` 再次校验同一精确身份，并从数据库读取 `ingestion_mode=automatic` 的固定上下文，不能把手动批次送进自动确认。
- `rust/apps/api/src/imports.rs:1401-1445` 的普通 `/confirm` 明确要求 `ingestion_mode=manual` 并传 `ImportConfirmationScope::manual`；`rust/crates/database/src/imports.rs:252-264,1595-1601` 在数据库层再次保持 manual/generic 与 automatic/non-generic/skip 两个 scope 不相交。
- `git diff 2a6e6d5..944a4de -- frontend` 无差异，手动导入 UI 未改；普通人类用户通过 UI 的 manual confirm 仍存在。
- 但“UI 未改”不等于服务端权限隔离。collector 仍是普通 analyst，可省略 automatic metadata 后创建并确认 manual 批次；普通 analyst 又可通过通用 inspect/mapping/preview/validate 修改 automatic 批次，随后由 collector 的 automatic-confirm 入队。详见 HIGH-05。
- E2E 没有身份 × mode × endpoint 的拒绝矩阵，也没有断言 automatic 批次不存在客户端 preview/mapping 旁路；这解释了该缺陷为何通过现有部署验收。

## 7. DEC-041 边界

- fallback 严格只允许 DCE：`DCE_FALLBACK_SOURCE` 唯一聚合定义；`AkshareAdapter._require_dce` 对非 DCE 直接拒绝；数据库 automatic source 白名单与迁移只授权 `akshare_sina_dce_fallback` 为 `aggregator_public/whitelisted_exception`。
- 官方优先真实存在：Runner 对每个 DCE dataset 先 `_collect_with_retries(source, fallback=false)`，失败后才调用 fallback；VPS extraction audit 同时看到 DCE official failed 与随后 Sina succeeded。
- 来源没有伪装：VPS 行情 DCE 200 行和席位 DCE 5340 行都关联 `akshare_sina_dce_fallback`；DCE official facts 为 0。SHFE/CZCE/GFEX/CFFEX 只关联各自 official source。
- 其余四所没有聚合源：代码、数据库自动源白名单和生产 `data_sources` 聚合例外均未发现第二个 aggregator。
- 边界仍因 HIGH-02 无法在官方恢复时自动切回，并因 HIGH-04 允许部分 fallback 数据被标记成功，故整体为 PARTIAL。

## 8. 安全与数据治理

| 检查项 | 结论 | 证据 |
| --- | --- | --- |
| collector drop root | PASS | 凭据以 root 读取后调用 `setgroups([]) → setgid(10001) → setuid(10001)`，再创建网络客户端；VPS 镜像 Config.User 为空，说明以 root 启动后执行降权。容器 cap 仅临时保留 SETUID/SETGID，且 no-new-privileges。建议后续 E2E 增加实际 UID=10001 断言。 |
| 凭据权限与挂载 | PASS | 宿主 collector credentials root:root 0400；容器单文件只读挂载；cron 0600、runner 0700；collector 不在 backend 网络。 |
| 凭据泄漏 | PASS | collector 错误日志只输出安全错误码；VPS collector/evidence/service log 关键字扫描 0，审计 metadata 禁止敏感 key 扫描 0；本评审未读取或输出任何密钥值。 |
| SQL 注入面 | PASS | 四类 dataset 的 SQL 为静态语句并使用 sqlx bind；枚举、dataset、来源和字段模板均固定，未发现用户值拼接进 SQL。 |
| RLS | PASS | `202608020001/02` 已应用；11 张新 workspace 表 11 ENABLE、11 FORCE、11 policy、11 grant。使用 `futures_app` 超级用户核总数；再切换 `futures_runtime` 且显式零 workspace 上下文得到 0，避免把 RLS 假 0 当生产空表。 |
| 出站网络安全 | FAIL | 直接 host/IP 校验存在，但逐跳重定向与 rebinding 不成立，见 HIGH-01。 |

## 9. E2E 断言有效性

### 四个已披露缺陷的复核

| 历史缺陷 | 当前实现 | 结论 |
| --- | --- | --- |
| 固定假设 127 批次 | baseline 改为 manual count `>=127`，并保存全部 manual batch fingerprint；结束时计数与 fingerprint 均相等 | 已修复；127 个历史批次未被修改，新增的 17 个 manual 批次不会造成恒假。 |
| 吞掉故障注入退出码 | 后台 PID 经 `wait "$pid" || status=$?` 捕获，helper `return "$status"`；故障运行若返回成功会显式 exit 1 | 已修复；反向强制非零退出会失败。 |
| 内存采样恒为 0 | 先读取 cgroup memory.peak/max_usage，失败才用 docker stats；最终断言 peak `>0` 且 `<=536870912` | 已修复；VPS 实测 130641920 bytes；反向 0 会失败。 |
| 误判用户数据变更 | 比较 users count 与去除 `updated_at/last_login_at` 后的稳定身份 fingerprint，不再把登录活动当业务变更 | 已修复；反向改变稳定字段会失败。 |

### 恒真、吞错与宽松匹配审查

- 未发现上述四个修复点仍为恒真断言；helper 内两处 `|| true` 仅用于允许采样手段降级，最终 `peak > 0` 保证不能把采样失败伪装成 PASS。
- 主 collector、replay 和故障 helper 的退出码没有继续被最后一条成功命令覆盖。
- 仍存在来源集合宽松匹配和回滚/授权矩阵遗漏，见 MEDIUM-04；因此不能仅凭唯一 PASS marker 推导全部契约已覆盖。

### 三个反向验证

| 反向条件 | 实际结果 | 判断 |
| --- | --- | --- |
| 将 collector helper 的等待状态构造为 1 | `reverse_exit_status=EXPECTED_FAIL observed=1` | 断言真实失败，不吞退出码。 |
| 将三个采样峰值构造为 0 | `reverse_zero_memory=EXPECTED_FAIL` | `peak > 0` 真实失败。 |
| 令结束时稳定用户 fingerprint 与 baseline 不同 | `reverse_fingerprint=EXPECTED_FAIL` | 用户数据变化断言真实失败。 |

反向验证只在本地抽取同等 shell 条件执行，没有修改仓库文件或 VPS 数据。

## 10. 本地门禁实际输出

| 命令 | 结果 | 实际输出摘要 |
| --- | --- | --- |
| `cargo +stable fmt --check` | PASS | exit 0，无输出。 |
| `cargo +stable clippy --workspace --all-targets -- -D warnings` | PASS | exit 0，workspace/all targets 完成，0 warning。 |
| `cargo +stable test --workspace` | PASS | 127 passed、0 failed；分组 22/13/66/13/8/5，doc-tests 通过。 |
| `pnpm lint` | PASS | `vue-tsc --noEmit` exit 0。 |
| `pnpm test` | PASS | 受控宿主复跑 6 files / 18 tests passed，约 4.36s。首次沙箱运行在 Vitest 启动前因 esbuild 无权读取工作区父目录失败，不计为断言失败。 |
| `pnpm build` | PASS | 1468 modules transformed，约 3.47s；仅既有的 >500 kB chunk warning。首次沙箱运行同样在构建启动前遇到 esbuild 权限问题。 |
| `ruff check collector/src collector/tests` | PASS | `All checks passed!`。 |
| `ruff format --check collector/src collector/tests` | PASS | `13 files already formatted`。 |
| `pytest`（工作目录 `collector`） | PASS | 15 passed in 0.56s；仅因评审环境禁止创建 `.pytest_cache` 的 warning。 |
| `git diff --check 2a6e6d5..944a4de` | PASS | exit 0，无输出。 |
| `git diff --check 2a6e6d5..HEAD` | PASS | exit 0，无输出。 |
| `docker compose config` | 本机不可执行 | 本机无 Docker CLI；同 SHA 的 CI validate 中开发/生产 compose config 均 success，VPS 生效配置另行只读核对。 |

Python/ruff 使用工作区外的一次性评审虚拟环境和 `collector/requirements-dev.lock`，没有向仓库写入依赖或缓存。一次组合命令曾因工作目录错误使 ruff 找不到路径、而后续 pytest 成功导致 shell 最终状态为 0；该结果明确作废，上表来自三个独立复跑命令。

## 11. GitHub Actions 与 VPS 只读实证

### GitHub Actions

- CI Run `30753685223`：success。`validate` 的 Rust fmt/clippy/test、Python 3.13.11 依赖安装、ruff check/format、collector tests、前端 lint/test/build、开发与生产 Compose config 全部 success；API、Worker、Frontend、Collector 四个 build job 全部 success。
- Container images Run `30753724067`：success。四个发布 job 全部 success，并产出不可变 digest。
- Deploy Run `30754021926`：success。精确 checkout `944a4de`、迁移、不可变镜像部署、Phase 4A E2E、证据记录与 registry auth cleanup 全部 success；日志恰有一个 `PHASE4A_E2E_PASS` 和 `DEPLOYMENT_PASS`。
- 镜像 digest：API `sha256:3ee25c7fd40c9f0e8c95caf8c3d068b8080a8d03e4fef29724c06c75e060abda`；Worker `sha256:960173e949be5c07c6d1d71c64bd4ed5ca8ade8739b85ed27447e9e7c8d414e3`；Frontend `sha256:deaa22ce164f7697e5319bbcc926ccf7321122cceee97ed6d9d838e244582875`；Collector `sha256:bcb8d75db3a94be6280438e79fdf9ef7b5b0cb26009f05db2cfcef85d0d5ab7d`。

### `futures` VPS

- `/api/v1/version` 经 nginx 端口 8088 返回完整 `git_sha=944a4defe578d5922b9f1ea83f951ddbd6fb005e`；API、Worker、Frontend、Nginx、PostgreSQL 容器运行，API/PostgreSQL healthy。最初对宿主 8080 的探测失败是端口选择错误，不是服务故障，已用实际 8088 复核。
- 最新 release 为 `/opt/futures-platform-releases/944a4defe578d5922b9f1ea83f951ddbd6fb005e-30754021926-1`；部署报告 `status=PASS`，runtime digest、registry auth cleanup、service log secret scan 均 PASS。
- `/etc/cron.d/futures-collector` 为 root:root 0600，包含 `CRON_TZ=Asia/Shanghai` 和工作日 17:30/21:30 两条任务；root crontab 不存在。runner root:root 0700，凭据 root:root 0400。
- 生效 collector 配置：指定上述 collector digest，`restart=no`、`read_only=true`、仅 edge 网络、无 depends_on、mem `536870912`、`cap_drop=ALL`、只加 SETUID/SETGID、no-new-privileges、凭据只读 bind；tmpfs 仅 `/tmp`。
- E2E collection date `2026-07-30`；峰值内存 `130641920` bytes；首跑、重放、故障注入、RLS、provenance、cron、用户不变均记录 PASS。

### 生产数据库只读聚合

核数使用 `futures_app`（`superuser=true,bypassrls=true`）并在需要时显式切换 runtime/workspace 上下文，未把无上下文 RLS 的 0 行假象作为总数。

| 实体/检查 | 实际值 |
| --- | ---: |
| `market_prices` | 818 |
| 行情来源/交易所 | SHFE official 302；CZCE official 240；DCE Sina fallback 200；GFEX official 48；CFFEX official 28 |
| `seat_positions` | 17806 |
| `contracts` | 1545 |
| manual import batches | 144 |
| automatic import batches | 78 |
| users | 32 |
| 行情/席位唯一键重复组 | 0 / 0 |
| 行情/席位 provenance 完整 | 818/818；17806/17806 |
| direct、change_log_version=1、succeeded 自动批次 | 64/64 |
| 上述批次 change rows | 20174 |
| 被事实引用但仍标为 direct 的目录/日历批次 | 5 / 5 |
| Phase 4A 新 workspace 表 RLS | 11 enable / 11 force / 11 policy / 11 grant |
| runtime + 零 workspace 上下文行情可见数 | 0 |
| 审计 metadata 敏感 key 命中 | 0 |

- 127 个历史测试批次按用户裁定保留：以 Phase 4A 前 cutoff 查询仍为 127，最大 `updated_at=2026-07-25 17:11:10.755764+00`，自 Phase 4A 部署窗口以来这 127 个批次更新数为 0。当前 manual 总数 144 的新增 17 条不改变“原 127 条未动”的结论。
- DCE extraction audit：official 的 catalog/market/seats/calendar 分别有失败记录，Sina 四类均有成功记录；事实来源与该审计一致。
- 本次所有 VPS 操作均为元数据、日志摘要和 SQL SELECT；没有调用写 API、没有触发采集、回滚、清理或数据修复。

## 12. 越界核验

| 边界 | 结论 | 证据 |
| --- | --- | --- |
| 未实施 Phase 4B 回填 | PASS | 代码差异没有历史日期范围采集、历史事实回填 job 或 Phase 4B 入口；只有单个显式 collection date。 |
| 未改手动导入流程 | PASS | 前端零差异；普通 `/confirm` 仍要求 manual mode 和人工请求，自动确认是独立 endpoint/scope。 |
| 未合并 main | PASS | `origin/main=2a6e6d5`，候选分支 merge base 同为 `2a6e6d5`；`main` 不含 `944a4de`。 |
| 未打标签 | PASS | 没有 tag 包含 `944a4de`。 |
| 未恢复已废止基础设施 | PASS | 差异未重新引入旧部署栈、旧凭据通道或已废止后台基础设施。 |
| 本评审未越界 | PASS | 只新增本报告并做本地/远端只读取证；未改业务代码、VPS 配置或数据。 |

## 13. Git 基线与分支状态

- 评审开始时 `HEAD` 与 `origin/phase/04-akshare-collection` 均为文档提交 `1a27e07`；其父提交 `944a4de` 是固定代码候选。
- `git diff 944a4de..1a27e07` 只有 Generator 交接文档；本报告不把该交接的声明当作结论。
- `origin/main` 与 merge base 都是 `2a6e6d5`。本单没有执行 merge、rebase、tag、PR 或 Phase 4B 操作。
- 报告提交后，分支只会额外增加本审查文档提交；代码结论仍严格截止 `944a4de`。

## 14. 最终判定

**FAIL**

- BLOCKER：0
- HIGH：5
- MEDIUM：4
- LOW：1
- 必须先修复 HIGH-01 至 HIGH-05，并为重定向目标未被访问、fallback→official 恢复、Phase 4A 全投影回滚、DCE 部分请求失败、身份与 ingestion mode 双向隔离增加真实反向回归。
- MEDIUM-01 至 MEDIUM-04 也应在最终 PASS 前关闭或由用户明确重新裁定契约；LOW-01 应补齐 `/work` tmpfs。
- 既有 CI、镜像、部署和 `PHASE4A_E2E_PASS` 是有效的已覆盖主路径证据，但不能覆盖本报告复现出的安全、来源恢复、回滚和完整性缺陷。
- 因结论为 FAIL，本评审不授权合并 `main`、打标签或启动 Phase 4B。

## 复核(2026-08-04)

### 复核范围、方法与结论摘要

- 角色与范围：全新独立 Evaluator；首轮报告固定为 `d30b3e3`，复核提交范围固定为 `d30b3e3..6c18fc2`，运行候选固定为 `82cec44`。`HANDOFF_20260804_1215.md` 只用于定位线索，没有采信其结论。
- 操作边界：本次只做本地静态审查、门禁复跑、GitHub Actions 只读取证和 `futures` VPS 只读查询；没有调用写 API、没有重部署、没有清理或修复生产数据。
- 结论：首轮十项缺陷中 **9 项 CLOSED、1 项 NOT-CLOSED**。HIGH-01、HIGH-02、HIGH-04、HIGH-05、MEDIUM-01 至 MEDIUM-04、LOW-01 已达到首轮“影响与建议”的关闭标准；HIGH-03 仍漏掉一条正式存在的下游外键依赖，因此 Phase 4A 最终仍为 **FAIL**。

### 十项逐一复核

| 首轮项 | 复核结论 | 验收证据 |
| --- | --- | --- |
| HIGH-01 重定向/DNS 绕过 | **CLOSED** | `collector/src/futures_collector/sources.py:389-532` 对每一跳执行 HTTPS、精确白名单域名和公网 IP 校验，关闭 requests 自动重定向，并在实际 transport 调用期间把 `getaddrinfo` 绑定到刚复核的地址集合；连接前二次解析不一致即拒绝。定向 pytest 的私网 302、非白名单公网 302、多跳上限、解析漂移 4 个用例均通过，并分别断言禁用 URL 未进入请求列表或 transport 调用数为 0，而不只是断言最终抛错。 |
| HIGH-02 fallback→official 业务身份冲突 | **CLOSED** | `rust/crates/application/src/import_jobs.rs:385-461` 的四类自动数据业务键均前缀化受控 `data_source_code`；迁移 `202608030001/02` 在 Workspace/RLS 约束下补齐既有自动记录。E2E `rust/tests/phase_4a_e2e.sh:415-483` 覆盖 fallback→official 相同值和不同值：行情、席位各保留两来源各两行，fallback 历史行不改写，`preferred_*` 视图按 `data_sources.priority` 选择 official。VPS 当前 official priority=100、fallback=50，唯一聚合源仍只有 `akshare_sina_dce_fallback`。 |
| HIGH-03 正式投影回滚预检 | **NOT-CLOSED** | 八类投影已进入 v2 change log、快照/版本栅栏和逆序回滚；四类成功回滚、现有依赖冲突、陈旧预检、零变更路径均有 E2E，迁移 `496a2cb` 也把 5 个受事实引用的遗留目录批次和 5 个遗留日历批次安全归类为 `compensation_only`。但是 `projection_dependencies()` 对 `exchange` 只枚举 `instruments`，漏掉 schema 中真实的 `trading_calendar_versions(workspace_id,exchange_id) → exchanges` `ON DELETE RESTRICT` 外键。现有 E2E 使用已存在的 CFFEX，目录批次没有插入 exchange，故没有证明该边的“预检结论=执行结果”。详见下节。 |
| HIGH-04 DCE 部分数据误报成功 | **CLOSED** | DCE fallback 的行情请求/解析缺口、席位部分 rank 发布均累计 skip 后抛出完整性错误；Runner 不上传部分 CSV，记录整个 dataset failed。按用户裁定，不引入 partial；无该交易日 observation、整组合约无发布排名属于显式未发布并被排除，而请求/解析失败或部分发布使整批失败。相关 pytest 与 Deploy E2E fault 分支通过。 |
| HIGH-05 automatic/manual 授权旁路 | **CLOSED** | 上传入口双向拒绝 collector→manual 与普通用户→automatic；inspect/mapping/preview/validate/manual-confirm 全部调用 `require_manual_batch_endpoint`。`automatic_confirm` 的精确 collector 身份拒绝位于任何 batch failure/状态写入之前（`f8b3f39`），Uploaded 批次无条件执行服务端固定 inspect→mapping→full validation，非可信中间态报 `automatic_pipeline_untrusted_state`。Rust 拒绝先于变更测试和 E2E 身份×mode×endpoint 矩阵均通过。 |
| MEDIUM-01 交易日推断 | **CLOSED** | 新增版本化 2026 交易日历；无 `--date` 时宿主先以受控日历解析日期，再把显式 `--date` 传入一次性容器，跨年/未受控年份拒绝。节假日、周末、节后首开日和超范围 pytest 均通过。 |
| MEDIUM-02 `observed_at` 固定值 | **CLOSED** | 每个 exchange/dataset 在网络采集前获取一次 UTC 时间并贯穿该批，normalize 要求 timezone-aware；单测验证时间位于调用窗口且批内一致，E2E 还验证正式行情 observation 不晚于 batch commit。 |
| MEDIUM-03 目录非空值不补齐 | **CLOSED** | exchange/instrument/contract 采用受控字段 upsert，真实变化递增 `row_version` 并记录 before/after snapshot；E2E 验证参数补齐及回滚恢复原空值。 |
| MEDIUM-04 E2E 覆盖/断言过宽 | **CLOSED** | 首跑和故障运行均使用精确 expected source set；完整身份矩阵、四类投影成功回滚、依赖冲突零变更及 DCE 双来源恢复已加入。另抽取同等 shell 条件做三项反向验证：缺少一个官方源、peak=0、manual fingerprint 漂移均实际得到 `exit=1 expected=1 => PASS`。该 E2E 仍未捕获 HIGH-03 的特定缺边，但首轮 MEDIUM-04 要求的精确集合和三类恒真/吞错风险本身已经关闭。 |
| LOW-01 缺少 `/work` tmpfs | **CLOSED** | `docker-compose.yml:92-96` 配置 `/work:size=128m,mode=0700,uid=10001,gid=10001`，并固定 `COLLECTOR_TEMP_ROOT`/`TMPDIR=/work`；E2E 对精确配置作断言。 |

### 剩余 HIGH-03：缺失的 exchange→calendar version 依赖边

**证据**

1. `rust/migrations/202608020001_phase_4a_collection_schema.sql:129-130` 明确定义 `trading_calendar_versions(workspace_id, exchange_id)` 引用 `exchanges(workspace_id,id) ON DELETE RESTRICT`。
2. `rust/crates/database/src/imports.rs:2791-2853` 的 `projection_dependencies()` 在 `target_kind="exchange"` 分支只查询 `instruments`；没有查询 `trading_calendar_versions`。同一函数已覆盖 instrument→contract、contract→market/seat、calendar version→day/market、seat entity→seat position，说明这不是由通用 FK 枚举自动补足的边。
3. 预检只把该函数返回且不属于同批 inserted targets 的对象报告为 downstream dependency（`rust/crates/database/src/imports.rs:2429-2449`）。因此缺失边不会出现在 conflict 清单或 fingerprint 中。
4. Worker 对 insert change 会执行 `delete from exchanges`（`rust/crates/database/src/rollback_jobs.rs:334-341`）；日历版本仍存在时数据库才以 FK restrict 拒绝。结果是“预检可回滚、执行期失败”，不满足首轮要求的预检可信度与零副作用拒绝。
5. `rust/tests/phase_4a_e2e.sh:349-411` 在既有 CFFEX 上创建 instrument/contract，精确 change-set 断言为 `contract,instrument`，没有 exchange；catalog dependency 检查由后续 contract facts 触发，不能覆盖只剩 calendar version 引用 exchange 的场景。

**只读复现推演**

1. 在空目录的新 Workspace 导入 catalog，使同一批创建 exchange、instrument、contract。
2. 再导入只引用该 exchange 的 calendar version/day，不创建行情或席位。
3. 对 catalog 批次执行 rollback-check：instrument 是原 catalog 同批 inserted target，会被忽略；缺失的 calendar-version 查询使 exchange 不产生 downstream conflict，预检可返回 `can_rollback=true`。
4. 入队执行时先逆序删除同批 contract/instrument，随后删除 exchange；仍存的 calendar version 触发 `trading_calendar_versions_exchange_fk`。事务本身会回滚，但预检结论与执行结果不一致。

**关闭标准**

- 在 exchange 分支锁定并枚举 `trading_calendar_versions`，并审计八类投影的完整反向 FK 图；增加隔离 E2E：新 Workspace 首次 catalog 创建 exchange，后续仅 calendar 引用，catalog rollback-check 必须返回具体 downstream conflict，enqueue 必须拒绝且前后 fingerprint 不变。修复后还应保留现有四类成功回滚、陈旧预检和零变更覆盖。

### 对抗性专项结论

| 专项 | 结论 | 实际证据 |
| --- | --- | --- |
| HIGH-01 四个反向目标未收到请求 | PASS | 定向 pytest：`4 passed, 5 deselected`。两个 302 禁止目标断言 requested list 只有初始白名单 URL；多跳在上限前逐跳校验并证明超限下一跳未请求；DNS public→private 漂移断言 transport 调用列表为空。 |
| HIGH-02 同值/异值来源恢复 | PASS | E2E 为行情/席位分别创建 same/different 两组 fallback→official，事实表保留 4+4 双来源行，8 条 imported record 业务键均带来源；preferred 行为选择 official 的相同值或 official 的新值。生产当前 business-key 唯一重复组 0/0。 |
| HIGH-03 全投影、依赖、reclassify | **FAIL** | 八类 change log 和四类成功回滚成立；5+5 个此前被事实引用的 v1 目录/日历批次均为 `compensation_only`，没有被伪造为 v2。独立缺边使全部下游 FK 枚举与“预检=执行”仍不成立。 |
| HIGH-05 身份×mode×endpoint | PASS | collector 创建 manual、普通用户创建 automatic、双方交叉访问五个手动处理端点及两类 confirm 均拒绝；ordinary automatic-confirm 的拒绝在失败状态写入之前，固定流水线不能由 PreviewReady 等中间态绕过。 |
| MEDIUM-04 三项反向断言 | PASS | 实际输出分别为 `exact-source-set-missing-one: exit=1`、`zero-memory-sample: exit=1`、`manual-batch-fingerprint-drift: exit=1`；没有恒真断言或吞掉退出码。 |

### 本地门禁复跑

环境：`rustc/cargo 1.96.0`、Node `24.18.0`、pnpm `11.9.0`、Python `3.12.13`、ruff `0.14.14`。Python 依赖严格取自 `collector/requirements-dev.lock`，虚拟环境位于系统临时目录，仓库未新增环境文件。

| 门禁 | 实际结果 |
| --- | --- |
| `cargo fmt --all -- --check` | PASS，exit 0，无差异 |
| `cargo clippy --workspace --all-targets -- -D warnings` | PASS，4 个主要 crate 检查完成，0 warning/error |
| `cargo test --workspace --all-targets` | PASS，135 passed、0 failed（24/14/71/13/8/5） |
| `pnpm lint` | PASS，`vue-tsc --noEmit` exit 0 |
| `pnpm test` | PASS，6 files、18 tests passed |
| `pnpm build` | PASS，1468 modules transformed；仅既有 chunk-size warning |
| `ruff check collector` | PASS，`All checks passed!` |
| `ruff format --check collector` | PASS，15 files already formatted |
| `pytest -q collector/tests` | PASS，35 passed in 1.85s |
| HIGH-01 定向 pytest | PASS，4 passed、5 deselected |
| `git diff --check d30b3e3..6c18fc2` | PASS，exit 0 |
| Compose config | 本机无 Docker CLI，不能声称本地执行；同一 runtime SHA 的 CI Run `30813165664` 中开发/生产 Compose config step 均 success，VPS 生效配置另作只读核验。 |

前端 test/build 首次在工作区沙箱内因 Windows 对上层目录拒绝读取而启动失败；在同一 checkout 的受控宿主环境复跑均 exit 0。该失败没有测试用例或编译错误，不计为代码门禁失败。

### GitHub Actions 证据链

| Run | 绑定提交 | 结论与证据 |
| --- | --- | --- |
| CI `30813165664` | `82cec44184ffb6ae4bf700afd0210193a081ad0a` | success；validate 的 Rust/Python/前端/Compose 全步骤和 API/Worker/Frontend/Collector build jobs 全部 success。 |
| Container images `30813606780` | `82cec44184ffb6ae4bf700afd0210193a081ad0a` | success；四个不可变镜像 publish job 及 digest 记录步骤全部 success。 |
| Deploy `30873823206` | acceptance `579017b`；runtime `82cec44` | self-hosted job success；显式校验 runtime/acceptance revision，应用至 `202608030003`，日志唯一 `PHASE4A_E2E_PASS`，峰值 `135401472` bytes，runtime digest match/registry auth cleanup/service log secret scan 均 PASS。 |

- `579017b` 与 `6c18fc2` 的 hosted CI 分别为 Run `30873793685`、`30877288724`；各 job 的 `steps=[]`，Check annotation 明确为付款失败或 spending limit，未启动任何门禁。按用户裁定这不是缺陷，也不能替代 `82cec44` 已成功的业务候选 CI/镜像证据。
- `346d542` 之后 Deploy 允许 acceptance harness 与 runtime 分离；运行中 API `/version` 和 release 路径共同证明实际业务镜像仍是 `82cec44`，没有把 `579017b` 的验收脚本修订误当运行代码。

### VPS 只读实证

核数使用 `futures_app`（superuser=true）避免 RLS 无 Workspace 上下文的 0 行假象；本节全部是 `GET`、容器 inspect、systemd/cgroup 读取或 SQL `SELECT`。

| 检查 | 2026-08-04 实际值 |
| --- | --- |
| 运行版本 | `/api/v1/version.git_sha=82cec44184ffb6ae4bf700afd0210193a081ad0a` |
| API digest | `sha256:ead8f733e704412ccebdbb31a83c78ebff76304140fdaf07d588aed752906587` |
| Worker digest | `sha256:95170cdb58a0977a13c2342d598ec5169523e986e738bfa6d10faa19b654bd2a` |
| Frontend digest | `sha256:77c93db6cca3deefbcf9932010f03cbfa11fee63775c582f77ee778b413d483e` |
| Collector digest | `sha256:3bc5ce53760de7ba3e77ce1450aef2a561695b9d612fc526f070e81d02e16544`（release compose 锁定；一次性服务当前无常驻容器） |
| 迁移 | `schema_versions.version=202608030003` 精确 1 条，applied_at `2026-08-04 03:07:43.965069+00` |
| 正式事实 | `market_prices=830`，`seat_positions=17806`；两类业务唯一键重复组均为 0 |
| 来源 | 行情 SHFE 302/CZCE 240/GFEX 48/CFFEX 28/DCE fallback 212；唯一 `aggregator_public` 为 `akshare_sina_dce_fallback`，其余四所没有聚合源 |
| manual/users | manual batches=144，users=32 |
| manual 全量 fingerprint | 144 条当前锚点 `83ff6f72e7c3b01841d7a96040249d8f`；Deploy E2E 在 144 基线上做前后全行 fingerprint 相等断言并 PASS |
| 原 127 条 baseline | 按 Phase 4A 前 created cutoff 仍为 127；fingerprint `8c622394fa847521bf221d2dd17aac2b`；最大 updated_at 仍为 `2026-07-25 17:11:10.755764+00`，cutoff 后更新数 0 |
| 幂等重放 | Deploy 日志：market `830→830, new=0`；seat `17806→17806, new=0` |
| 遗留错误 direct 分类 | 受事实引用的 legacy catalog 5、calendar 5 均为 `compensation_only`、`change_log_version IS NULL`，未伪造 v2 change log |
| Phase 4A 投影 RLS/DELETE | 8/8 投影表 ENABLE RLS、8/8 FORCE RLS；在 11 张 Phase 4A 新表范围内，runtime DELETE 精确只有 exchanges/instruments/contracts/trading_calendar_versions/trading_calendar_days/market_prices/seat_entities/seat_positions 这 8 张投影表。runtime 对 Phase 3 的 imported records/staging/errors 既有 DELETE 不属于本迁移扩权。 |
| self-hosted runner | service active/running、Result=success、NRestarts=0；MemoryMax=268435456、MemoryPeak=268435456，`memory.events max=105` 如实反映触顶限流；oom/oom_kill/oom_group_kill 均为 0，相关 journal 命中 0 |
| cron | `/etc/cron.d/futures-collector` 仍为 Asia/Shanghai 工作日 17:30/21:30；没有触发本次复核采集 |

### 越界复核

| 边界 | 结论 | 证据 |
| --- | --- | --- |
| 未启动 Phase 4B | PASS | `d30b3e3..6c18fc2` 没有历史日期范围采集/回填入口；文件名中的 `202608030002_phase_4a_rls_backfill.sql` 是 Phase 4A RLS/分类修复，不是业务历史数据回填。 |
| 未清数据 | PASS | VPS 只执行只读命令；144/127 fingerprints、事实行数和重放证据均保留。 |
| 未恢复废止设施 | PASS | 差异未重新引入旧部署栈或旧凭据通道；部署迁移到用户授权的 self-hosted runner，不等于恢复废止基础设施。 |
| 四所无聚合源 | PASS | `aggregator_public` 精确只有 DCE Sina fallback；SHFE/CZCE/GFEX/CFFEX 均只存在 official source。 |
| 未合并/未标签/未重部署 | PASS | 复核开始时只有 `origin/phase/04-akshare-collection` 包含 `6c18fc2`，没有 tag 包含该提交；本单没有执行 merge、tag、workflow dispatch 或任何部署命令。 |

### 复核最终判定

**FAIL**

- BLOCKER：0
- HIGH：1（HIGH-03 NOT-CLOSED）
- MEDIUM：0
- LOW：0
- CLOSED：9 / 10

`PHASE4A_E2E_PASS`、CI、不可变镜像和生产数据主路径证据均有效，但不能覆盖缺失的 exchange→calendar version 回滚依赖边。Phase 4A 在补齐该边并增加隔离反向 E2E 前不能判定 PASS。

后续路线：先只修复并重新独立复核 HIGH-03。若后续复核达到 PASS，**仍不立即合并 main**；Phase 4B 在同一分支继续，Phase 4 整体完成后一次收口。届时 hosted CI 额度应已恢复，并在 merge commit 上补跑完整 CI。当前 FAIL 不授权合并 main、打标签、启动 Phase 4B 或重部署。

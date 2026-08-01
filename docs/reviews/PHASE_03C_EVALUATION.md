# Phase 3C 独立评审报告

评审日期：2026-07-26\
评审范围：仅 Phase 3C（校验、业务唯一性、四种冲突策略、确认入库、PostgreSQL `job_queue`、Worker、SSE、重试/并发/幂等、Workspace/RLS/审计及最小确认/进度前端）\
评审方式：独立 Evaluator 静态审阅、本地门禁复跑及 `futures` VPS 只读复核\
最终结论：**PASS**

## 1. 最终结论

- BLOCKER：0
- HIGH：0
- Phase 3C 授权范围与第 8.3 节退出门禁已满足。
- 未实现回滚、补偿批次、cancel/dead-letter replay、行情、交易、套利、图表、OCR、AI 或外部网站采集。
- Phase 3B 评审列出的五项 MEDIUM 均已在本轮范围内收口；没有借机扩展 Phase 3D。

## 2. 初审问题与修复复核

初审发现的 HIGH 均已修复并重新验证：

1. Worker 原实现长时间持有 `job_queue` 行锁，阻断续租和过期租约恢复。最终实现使用 `lease_generation` 代际 fence，长事务不持有 job/batch 锁，提交边界统一按 job → batch 锁序校验；真实旧代执行与新代重领重叠时，旧代正式写和终态均被回滚。
2. mapping 声明的 transform 曾未在正式校验中执行。最终预览与完整解析共用确定性 transform，未实现的参数化 transform 不再被广告或接受；受控 decimal 校验拒绝 `NaN`、无穷值及指数语法。
3. 校验、确认失败及 Origin/CSRF/权限拒绝的审计曾不完整。最终成功、失败、拒绝、幂等重放、Worker 成功/失败/dead-letter 均有脱敏审计。
4. workspace 级幂等键在不同批次并发使用时曾可能冒泡唯一约束错误。最终使用 workspace + 哈希幂等键的事务级 advisory lock，并维持 advisory key → batch 锁序；并发结果稳定收敛为一个 `202` 与一个 `409 idempotency_key_reused`。
5. 版本化 E2E 最初没有覆盖硬性并发与策略矩阵。最终脚本覆盖 4 策略 × 3 冲突来源、20 路并发确认、双 Worker、续租、SIGKILL 后恢复、旧代 fence、瞬时重试、永久错误及第五次 dead-letter。

## 3. 关键不变量

### 校验、唯一性与冲突策略

- 完整解析校验覆盖字段、跨字段、受控引用、业务键、blocking error、warning、文件内 duplicate 和既有数据库 conflict。
- `imported_records` 以 `(workspace_id, dataset_type, business_key)` 强制业务唯一；独立数据库复核未发现重复业务键。
- `skip` 保留第一条/既有记录，`overwrite` 选择最后候选并递增 `row_version`，`keep_conflict` 仅写 `import_conflict_candidates`，`abort` 不留下 job 或部分正式效果。
- 校验后并发插入 conflict 的四策略均经过真实 Worker 路径验证。

### 确认、Worker 与幂等

- confirm 在单事务中冻结参数、写确认身份、唯一 job、queued 事件与审计。
- 同键同参、同键异参、不同键同参、不同键异参和跨批次同键竞争均有测试；20 路并发确认只形成一个 job 和一份正式效果。
- Worker 使用 `FOR UPDATE SKIP LOCKED`；租约每 10 秒续期，`lease_generation` 防止旧执行者提交。
- SIGKILL 后保留的真实租约自然过期，重启 Worker 后以第二代恢复成功。
- 可重试 `40001` 成功进入第二次尝试；确定性 `abort_conflict` 仅执行一次；连续五次 `40001` 只在第五次进入 dead-letter。
- job 终态、批次终态、正式数据、计数、终态事件和审计同事务提交；未发现重复终态或事件序号缺口。

### SSE、Workspace/RLS 与审计

- SSE 支持首次读取、`Last-Event-ID` 精确重放、非法/跨批次游标拒绝、跨 Workspace 不可见及终态关闭。
- 五张 Phase 3C 新表全部启用并强制 RLS；`futures_runtime` 为非 superuser、无 `BYPASSRLS`，跨 Workspace SELECT/UPDATE/INSERT 破坏性测试通过。
- 独立复核确认 confirmation → job 绑定无错配，事件 payload 与审计 metadata 不含原始行、幂等键、Cookie/Token、CSRF 值或 `record_data`。
- Worker 审计 actor 与冻结的 `confirmed_by` 一致。

## 4. Phase 3B MEDIUM 处理

1. 前端 inspect 仅在服务端确认预览失效时清理预览/errors，并有 helper 测试。
2. errors 改为绑定 Workspace、批次和稳定排序键的游标分页。
3. 映射写入失败时 preview/staging/errors/status 的事务回滚由数据库脚本覆盖。
4. 模板 `dataset_type` 冻结竞争由双连接脚本覆盖。
5. Phase 3B 数据库脚本的迁移前置说明已更新到实际依赖版本。

## 5. 最终证据

### 本地

- 分支：`phase/03-import-foundation`；评审时基线 HEAD：`4cc1722`，Phase 3C 尚未提交。
- `git diff --check`：PASS。
- Rust：fmt PASS；workspace tests 47 passed；clippy `-D warnings` PASS。
- 前端：lint PASS；Vitest 3 files / 5 tests PASS；生产 build PASS。仅保留既有的大 chunk warning。
- `bash -n rust/tests/phase_3c_e2e.sh` 由 Generator 及 VPS Bash 验证通过。

### `futures` VPS

- 源码归档 SHA-256：`65addb91e0cc7dff0852d3245c2e29d3341bac6549c7f1ae9e79098af5d1c417`；随后只 overlay 测试脚本修复，未改变镜像业务代码。
- 最终 E2E 脚本 SHA-256：`434c291ec01f1fb2813b1c2c2477a4735a493c42351e85db0ab589b4f8988a00`。
- 最终脚本 exit `0`，输出 `PHASE3C_E2E_PASS`；证据目录：`/tmp/phase3c-e2e-559842`。
- 12/12 冲突矩阵、75 行正式导入、20 路并发确认、幂等四组合与跨批次竞争、transform/formula、errors cursor、SSE、5/5 RLS 读写、双 Worker、续租、SIGKILL 恢复、generation fence、永久错误、瞬时重试、第五次 dead-letter、审计和秘密扫描全部通过。
- PostgreSQL 迁移 `202607250008`、`202607250009` 已记录；Phase 3B mapping SQL、模板冻结并发脚本和 Phase 3C runtime grants 脚本通过。
- API、PostgreSQL healthy；live、ready、version、首页均 HTTP 200；五个容器运行。
- 独立数据库复核：重复业务键 0、重复终态 0、事件序号缺口 0、confirmation/job 错配 0、敏感审计 key 0、敏感事件 key 0、错误 Worker actor 0、临时 E2E trigger 0、禁建表 0。
- 根分区尚余约 7.1G；主密钥文件为 `0400 root:root`。

## 6. 非阻断 MEDIUM / 后续风险

1. SSE OpenAPI 已注册事件组件，但 `text/event-stream` 的 200 response 没有直接表达逐帧事件 schema；`event_not_visible` 枚举当前也没有独立响应路径。
2. Worker 按排序后的 Workspace 逐个寻找任务；持续高负载时缺少显式的跨 Workspace 公平轮询策略。
3. SSE 在建流前校验 Session/权限，流建立后不会周期性重新验证会话；当前个人 Workspace、短任务模型降低了影响，后续长任务或动态成员权限需要补强。
4. 前端测试覆盖状态 helper、SSE parser/header 和基础 App，但确认面板与断线重连仍缺少组件级交互测试；后端真实 E2E 已覆盖对应协议路径。

以上均不构成本阶段 BLOCKER/HIGH，不影响 Phase 3C PASS；应在后续授权阶段显式排期。

# 最新交接状态

- 最新交接文档：`docs/handoffs/HANDOFF_20260804_1527.md`。
- 当前阶段：Phase 4A 复核残留 HIGH-03 已形成本地修复候选，尚未完成 hosted
  CI→镜像→Deploy→E2E→独立 Evaluator 单项终验，因此不得宣告 PASS 或启动 4B。
- Git：分支 `phase/04-akshare-collection`；基线 `c866ff0`；修复候选
  `23e679db3b7fa3a384e764235e6ea3066d18766f`。
- 修复：exchange 回滚预检已补齐 `trading_calendar_versions` 的 `for update` 依赖查询；
  Phase 4A 正式投影 8 条反向 FK 已全量对照，无第二条遗漏。
- 测试：Rust fmt/clippy/workspace tests、前端 lint/18 tests/build、Python ruff/35 tests、
  E2E shell 语法与 diff check 均通过；新 PostgreSQL 集成用例已编译，实际 DB 执行
  等 hosted CI 额度恢复。
- VPS：未部署，运行版本仍为 `82cec44`；self-hosted runner 已从 256 MiB 调至
  384 MiB，当前 active/running。
- 下一步：额度恢复后严格依次执行 CI、四镜像、Deploy、Phase 4A E2E，再由独立
  Evaluator 终验该单项。

接管者必须完整阅读最新交接，以 Git、Actions 和 VPS 实态独立取证；不得输出密钥，
不得重部署旧候选，不得合并 main、打标签或启动 Phase 4B。

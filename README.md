# 期货与套利数据分析平台

本项目面向个人或小团队，建设一个本地部署的期货数据研究、套利分析、交易复盘、席位分析和 AI 辅助分析平台。平台只提供数据管理与分析能力，不连接期货账户自动下单。

Phase 1 至 Phase 3 已完成版本收口并经独立 Evaluator PASS。Phase 4A 的五交易所 akshare 单日自动采集、正式业务表入库、DCE 官方优先/Sina fallback、幂等重跑、故障隔离与 VPS 定时调度也已完成实现和部署，候选 `944a4de` 等待独立 Evaluator；Phase 4B 全历史回填尚未实施。Phase 3 已以普通 merge commit `33aa838` 合入 `main`，标签为 `phase-3-pass-20260801`。

## 已确认技术基线

| 项目 | 选择 |
| --- | --- |
| 前端 | Vue 3、TypeScript、Vite、Element Plus、Pinia、ECharts |
| 前端包管理器 | pnpm |
| 后端 | Rust 1.96.0；由 `rust-toolchain.toml` 固定 |
| Web/API | Axum、Tokio、Serde、Tower、Tracing |
| 数据库访问 | PostgreSQL、SQLx |
| 登录 | Cookie Session |
| 文件存储 | 第一版本地存储适配器，预留 S3 兼容适配器 |
| 后台任务 | PostgreSQL 任务表 |
| 自动采集 | 独立 Python akshare 容器，盘后按需运行，标准化 CSV 调用导入 API |
| API 文档 | OpenAPI、Utoipa |
| 测试 | Vitest；Cargo Test；SQLx 测试数据库 |
| 部署 | Docker Compose |
| 数据库命名 | `snake_case` |
| API 前缀 | `/api/v1` |

具体依赖版本由 `rust/Cargo.toml` 和 `frontend/package.json` 锁定；生成的锁文件以本地验证结果为准。

## 项目边界

第一版包含：

- 用户登录、角色权限和审计。
- TXT、CSV、XLS、XLSX 导入，字段映射、预览、校验、冲突处理和批次回滚。
- 交易所、品种、合约、交易日历等基础数据。
- 多腿套利定义、价差序列和可复核统计。
- 成交、交易组和每日持仓快照的录入与统计。
- 席位原始数据、别名、分类版本和汇总分析。
- 交互图表及 PNG/SVG 导出。
- 五交易所 akshare 自动采集、全部可得历史回填和来源/批次/回滚追溯。
- 受控、只读、可追溯的多模型 AI 分析。

第一版不包含自动交易、绕过访问控制、通用无规则网页抓取、微服务/Kubernetes/Kafka、多租户 SaaS、分钟级行情和完整策略回测。

## 已确认业务与架构口径

- 数据归属于个人 `Workspace`；所有业务数据通过 `workspace_id` 强制隔离，MVP 不实现共享和邀请。
- 时间点以 UTC/`timestamptz` 保存，业务时区为 `Asia/Shanghai`；夜盘按交易所日历归入下一交易日。
- 收盘价与结算价分别保存；不同分析结果必须标记 `price_basis`。
- MVP 不自动生成连续合约，不提供规则驱动的历史回测。
- 原始成交不可覆盖，默认 FIFO 配对，胜率按完整 `trade_group` 计算。
- 普通业务实体使用 UUIDv7，高频时间序列与明细使用 BIGINT identity。
- 金融最终存储使用确定的 `numeric` 精度，MVP 仅开放 CNY。
- 批次仅在无后续修改和依赖时整批原子回滚；纠错使用补偿批次。
- 敏感数据使用信封加密；自动采集容器仅允许访问五交易所域名白名单；仅 DCE 可在官方 412 等失败后切换到 DEC-041 许可的新浪域名白名单，并如实记录聚合来源。
- 白名单结构化自动批次免人工确认、免提取预览，失败批次隔离且不写正式表；手动文件导入流程不变。
- PNG/SVG 由 ECharts 前端直接导出，SVG 继续执行安全清洗。

完整约束见 [已确认决策](docs/DECISIONS.md)。

## 建议实施顺序

`Phase 1–3 基础、权限与导入（已完成） → Phase 4A 单日 akshare 采集（已实现，待 Evaluator） → Phase 4B 全历史回填（未实施） → Phase 5 多腿套利与图表 → Phase 6 成交/持仓/绩效 → Phase 7 席位分析 → Phase 8 AI → Phase 9 运维加固与完工事项`

该顺序修正了原方案“阶段 2 先做交易持仓、阶段 3 再做导入”与结论“先建设导入和数据层”的冲突。

## 文档

- [已确认决策](docs/DECISIONS.md)
- [产品需求](docs/PRODUCT_REQUIREMENTS.md)
- [总体架构](docs/ARCHITECTURE.md)
- [模块设计](docs/MODULE_DESIGN.md)
- [数据库设计](docs/DATABASE_DESIGN.md)
- [API 设计](docs/API_DESIGN.md)
- [安全设计](docs/SECURITY_DESIGN.md)
- [导入设计](docs/IMPORT_DESIGN.md)
- [AI 设计](docs/AI_DESIGN.md)
- [开发计划](docs/DEVELOPMENT_PLAN.md)
- [验收标准](docs/ACCEPTANCE_CRITERIA.md)
- [踩坑与教训](docs/PITFALLS.md)
- [当前计划](PLANS.md)
- [原始设计方案](docs/reference/期货与套利数据分析平台_完整设计方案_v1.0.docx)

## 本地启动与验证

准备环境：

```powershell
Copy-Item .env.example .env
pnpm install --frozen-lockfile
```

后端验证在 `rust/` 目录执行：

```powershell
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

前端验证在项目根目录执行：

```powershell
pnpm lint
pnpm test
pnpm build
```

Docker Compose 启动入口：

```powershell
docker compose --profile dev up --build
```

本地默认访问地址：

```text
http://localhost:8088
```

当前本机若未安装 Docker，Docker 相关命令会失败；该状态必须如实记录在验证结果中。

## 当前状态

- 原始 Word 方案已原样保存，SHA-256：`15FE19E6DC222F37B8CC0959A985423A1B02044EA07877F3562BDCEE5F6A9521`。
- 已完成方案内容审阅、Markdown 设计拆分和 `ce-doc-review` 文档审查。
- 架构与业务口径已确认并写入 `docs/DECISIONS.md`。
- 尚未确定的实现细节直接立 `DEC-xxx` 草案(原 OPEN_QUESTIONS.md 已删,开放项均已被 DEC 系列关闭)。
- Phase 1、Phase 2、Phase 3 均已完成并经 Evaluator PASS；Phase 3 main CI Run `30703979390` success。
- Phase 4A 候选 `944a4de` 的 CI Run `30753685223`、Container images Run `30753724067`、Deploy Run `30754021926` 全部 success；`futures` VPS 已运行该候选并返回 `PHASE4A_E2E_PASS`，现等待独立 Evaluator。
- Phase 4A 覆盖指定日期单次采集；Phase 4B 全历史回填、套利/分析/图表、AI 与新前端页面仍未实施。
- VPS 原 127 个历史测试批次仍未清理；部署前实态为 144 个手动批次，全部由数量与稳定行指纹保护。生产库归零重置，以及 TLS / `AUTH_COOKIE_SECURE=true` 生产验证，按用户裁定延后到项目完工时处理。
- 2026-08-02 总方案重审已确认：废止 `DEC-023`、`DEC-030`、`DEC-034`，修订 `DEC-025`、`DEC-031`，新增 `DEC-038` 至 `DEC-041`。

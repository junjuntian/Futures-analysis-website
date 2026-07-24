# Phase 1：工程基础建设

## 范围

Phase 1 只建立可运行、可测试、可部署的基础工程：

- Git 仓库、分支、`.gitignore` 和提交规范。
- Rust Workspace：`apps/api`、`apps/worker`、`crates/common`、`crates/domain`、`crates/application`、`crates/infrastructure`、`crates/database`。
- Axum API 基础：`/api/v1/health/live`、`/api/v1/health/ready`、`/api/v1/version`、OpenAPI 骨架、统一 JSON 响应、请求 ID。
- Worker 基础：PostgreSQL 连接、任务循环框架、优雅关闭、后续 `job_queue` 接口预留。
- Vue 3 前端基础：Vite、TypeScript、Pinia、Vue Router、Element Plus、ECharts、健康状态显示、404、全局错误处理。
- PostgreSQL 迁移基础：系统版本表、RLS 基础设施、UTC/Asia/Shanghai 规则说明、最小权限账户方案。
- Docker Compose：PostgreSQL、API、Worker、前端或前端开发服务、Nginx；预留 Playwright/noVNC/PaddleOCR。
- Nginx：前端静态资源、`/api/` 代理、`/events/` SSE、noVNC WebSocket 预留、安全响应头、上传大小和超时。
- CI：Rust、前端、Docker Compose 配置校验。
- 部署与维护文档：部署、发布、备份恢复、安全和贡献说明。

## 非目标

- 不实现行情、导入、套利、成交、席位、AI、采集、OCR 等正式业务功能。
- 不创建大量业务表。
- 不启用三禾数据连接器。
- 不实现任意 URL 通用抓取器。
- 不实现 Workspace 共享、邀请、自动交易、历史规则回测。
- 不推送 GitHub，除非已有明确远程地址。

## 目录结构

```text
.
├── .agents/
├── .github/workflows/
├── deploy/nginx/
├── docs/
│   ├── deployments/
│   ├── handoffs/
│   ├── phases/
│   └── reviews/
├── frontend/
├── rust/
│   ├── apps/
│   │   ├── api/
│   │   └── worker/
│   └── crates/
│       ├── application/
│       ├── common/
│       ├── database/
│       ├── domain/
│       └── infrastructure/
└── docker-compose.yml
```

## 数据流

1. 用户访问 Nginx。
2. Nginx 将 `/` 路由到 Vue 前端，将 `/api/` 路由到 Axum API，将 `/events/` 作为 SSE 代理路径预留。
3. API 读取配置、建立 PostgreSQL 连接池，提供健康检查和版本接口。
4. Worker 读取配置、连接 PostgreSQL，进入可停止的任务循环；本阶段不领取真实业务任务。
5. PostgreSQL 保存迁移版本和后续 RLS 基础设施；正式业务表留到后续阶段。

## 安全边界

- `.env`、Cookie、storage state、API Key、主密钥、数据库数据目录和用户上传文件不得进入 Git。
- 日志不得输出数据库连接串、`BOOTSTRAP_TOKEN`、主密钥或其他秘密。
- 数据库端口不暴露到公网。
- RLS 基础设施在第一张 Workspace 业务表前完成。
- 主密钥路径按 `DEC-029` 设计，只在部署阶段作为只读挂载。

## 任务拆分

| 顺序 | 任务 | 交付物 |
| --- | --- | --- |
| 1 | Git 初始化 | Git 仓库、`main`、`phase/01-foundation`、`.gitignore` |
| 2 | Rust 基础 | Workspace、工具链锁定、API/Worker/crates、基础测试 |
| 3 | API | live/ready/version、统一响应、OpenAPI、请求 ID |
| 4 | Worker | 生命周期、数据库连接、优雅关闭、任务接口预留 |
| 5 | 前端 | Vite/Vue 基础、健康状态、404、测试 |
| 6 | 数据库 | SQLx migration 基础、系统版本表、RLS helper |
| 7 | 容器与 Nginx | Docker Compose、Nginx 配置、`.env.example` |
| 8 | CI 与文档 | GitHub Actions、部署/发布/备份恢复文档 |
| 9 | 验证与部署 | 本地测试、futures VPS 核对和部署记录 |
| 10 | 审查与修复 | Evaluator 报告、BLOCKER/HIGH 清零 |

## 验收标准

- `cargo fmt --check`、`cargo clippy --workspace --all-targets -- -D warnings`、`cargo test --workspace` 通过。
- `pnpm install --frozen-lockfile`、`pnpm lint`、`pnpm test`、`pnpm build` 通过。
- `docker compose config` 通过。
- API `live` 只检查进程，`ready` 检查数据库连接，`version` 返回版本信息。
- Worker 可启动、连接数据库并优雅关闭。
- Nginx 代理、SSE 配置和 noVNC WebSocket 预留配置可被 Compose 校验。
- 没有秘密进入 Git、日志或文档。
- futures VPS 部署结果记录到 `docs/deployments/FUTURES_VPS_PHASE_01.md`。

## 验证命令

```powershell
git status --short
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm build
docker compose config
```

## 回滚方法

- Git 层：保留每个逻辑提交；需要回退时优先使用 `git revert`。
- 本地容器：`docker compose down` 停止服务；保留 PostgreSQL volume 供排查，除非明确执行带备份的数据清理。
- 数据库：Phase 1 仅有基础迁移；回滚以迁移工具的 down 脚本或前滚修正为准。
- VPS：部署前记录现有服务和端口；若部署失败，停止本项目容器并恢复部署前 Nginx/端口状态。

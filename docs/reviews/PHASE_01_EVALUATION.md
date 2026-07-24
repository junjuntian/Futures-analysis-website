# Phase 1 Evaluator 审查报告

## 审查结论

最终状态：PASS。

审查时间：2026-07-24 21:55 +08:00。

本次审查由主 Agent 按 Evaluator 范围内联完成。原因：已启动的 Evaluator 子 Agent 长时间未返回，超过合理等待后被中断。审查基于 Git、本地测试、部署文档和 `futures` VPS 实际验证结果。

## 审查范围

- `AGENTS.md`
- `PLANS.md`
- `docs/DECISIONS.md`
- `docs/phases/PHASE_01_FOUNDATION.md`
- `docs/deployments/FUTURES_VPS_PHASE_01.md`
- `docs/reviews/DOC_REVIEW_PHASE_0.md`
- Rust API/Worker 基础工程
- Vue/Vite 前端基础工程
- PostgreSQL migration
- Docker Compose 与 Nginx 配置
- `futures` VPS 部署结果

## 验证证据

### 本地验证

| 命令 | 结果 |
| --- | --- |
| `cargo +stable fmt --check` | 通过 |
| `cargo +stable clippy --workspace --all-targets -- -D warnings` | 通过 |
| `cargo +stable test --workspace` | 通过 |
| `pnpm install --frozen-lockfile` | 通过 |
| `pnpm lint` | 通过 |
| `pnpm test` | 通过；需真实文件系统权限，沙箱内受 esbuild 上级目录读取限制 |
| `pnpm build` | 通过；需真实文件系统权限，存在大 chunk 提示 |
| `docker compose config` | 本机未执行成功；本机无 Docker |

### VPS 验证

| 检查 | 结果 |
| --- | --- |
| Docker/Compose | Docker 29.1.3；Compose 2.40.3 |
| Compose 配置 | `docker compose --profile dev config --quiet` 通过 |
| API live | HTTP 200 |
| API ready | HTTP 200 |
| API version | HTTP 200 |
| 前端页面 | HTTP 200 |
| Nginx 代理 | 通过 |
| Worker | 运行中，已连接数据库 |
| PostgreSQL | healthy |
| 数据库迁移 | `202607240001` 已记录 |
| 重启测试 | 通过 |
| 停止/启动测试 | 通过 |
| `up -d --no-build` redeploy | 通过 |
| 日志秘密扫描 | 通过 |

## Findings

| 等级 | 编号 | 发现 | 处理 |
| --- | --- | --- | --- |
| HIGH | `P1-EVAL-001` | `frontend` 服务把项目目录只读挂载到 `/app`，同时挂载 `/app/node_modules` named volume，远端启动失败 | 已修复：将前端开发服务项目挂载改为可写，依赖写入 named volume |
| HIGH | `P1-EVAL-002` | 远端初始 `.env` 使用 base64 随机数据库密码，写入 `DATABASE_URL` 后可能因特殊字符导致 URL 解析失败 | 已修复：远端改为 hex 随机值；修复前已备份并重建空 PostgreSQL volume |
| MEDIUM | `P1-EVAL-003` | `HealthStatus.checked_at` 初始序列化为数组，与 OpenAPI 示例字符串不一致 | 已修复：启用 `time` 的 `serde-well-known` 并使用 RFC3339 序列化 |
| MEDIUM | `P1-EVAL-004` | `futures` VPS 内存约 956MiB，Rust release 构建存在 OOM 风险 | 已缓解：创建并启用项目 swapfile `/var/lib/futures-platform/swapfile` |
| LOW | `P1-EVAL-005` | 当前 Phase 1 部署使用前端 dev server，不是生产静态资源镜像 | 非阻塞：Phase 1 目标是基础工程验证；后续发布阶段应改为构建静态资源并由 Nginx 直接服务 |
| LOW | `P1-EVAL-006` | API `version.git_sha` 当前为 `local` | 非阻塞：后续 CI/镜像构建应注入实际 Git SHA |
| SUGGESTION | `P1-EVAL-007` | 本机无 Docker，Compose 校验依赖 VPS | 建议后续本机安装 Docker Desktop 或在 CI 中强制执行 Compose 校验 |

## 范围一致性

- 未发现行情、导入、套利、成交、席位、网页采集、OCR 或 AI 业务功能提前实现。
- 当前数据库只包含 Phase 1 foundation migration，不包含大量业务表。
- Workspace/RLS 决策未被实现阶段破坏；RLS helper 已创建，正式 Workspace 业务表尚未创建。
- 日志中数据库 URL 已脱敏。
- 远端主密钥文件权限为 `0400`，未进入 Git、镜像或日志。

## 可复现性判断

基础代码、Compose、Nginx 与 migration 均来自本地 Git 源码。远端 `.env`、主密钥和 swapfile 为部署环境状态，已记录在部署文档中，未写入明文秘密。

## 最终判断

PASS。

Phase 1 基础工程满足当前验收标准。剩余 LOW/SUGGESTION 不阻塞进入下一阶段，但在生产化部署前应处理前端静态化、Git SHA 注入和 CI/本机 Docker 校验。

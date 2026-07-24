# Phase 2 Evaluator 复核

评审时间：2026-07-25 01:13 +08:00

评审范围：首次用户初始化、个人 Workspace 隔离、Cookie Session、CSRF、权限基础、审计、PostgreSQL RLS、部署配置与验收证据。

非评审范围：行情、导入、交易、AI、OCR、外部数据采集、多租户协作邀请、生产 TLS 证书落地。

## 最终结论

PASS。

无剩余 BLOCKER/HIGH。Phase 2 可以提交。

## 审查过程

- 主 Agent 已按项目规则实际调用独立 Evaluator。
- 初次评审发现 1 个 HIGH：限速登录返回 429 前未写入 `security_events` 审计。
- Generator 已修复该 HIGH：`login` 在 `AuthError::RateLimited` 路径写入 `auth.login` / `denied` 审计后返回 429。
- Evaluator 复核确认 HIGH 已解除。

## 关键证据

### 本地验证

| 命令 | 结果 |
| --- | --- |
| `git diff --check` | PASS |
| `cargo +stable fmt --check` | PASS |
| `cargo +stable test --workspace` | PASS，4 个认证/权限基础单元测试通过 |
| `cargo +stable clippy --workspace --all-targets -- -D warnings` | PASS |
| `pnpm lint` | PASS |
| `pnpm test` | PASS；Windows sandbox 下 esbuild 读取 Vite 配置受限，已在同一项目目录非沙盒重跑通过 |
| `pnpm build` | PASS；仅 Vite chunk-size warning |

### VPS 部署与健康

| 项目 | 结果 |
| --- | --- |
| 源码传输 | Paramiko/SFTP 完整部署包上传；本地与远端 SHA-256 匹配 |
| 最新部署包 SHA-256 | `52c0577d2aef38bf2126f6b78d004e13af183683ac72daf7c45b16dd3e805230` |
| 部署前备份 | `source_pre_phase2_hotfix_20260724T170511Z.tgz` |
| `docker compose --profile dev config --quiet` | PASS |
| `docker compose --profile dev build` | PASS |
| `docker compose --profile dev up -d --force-recreate` | PASS |
| API health | `/api/v1/health/live`、`/api/v1/health/ready`、`/api/v1/version` 均 HTTP 200 |
| OpenAPI | `/api-docs/openapi.json` HTTP 200 |
| 重启恢复 | `api`、`worker`、`nginx` restart 后 API healthy，health/version 通过 |
| 日志秘密扫描 | suspicious secret hits = 0 |

### 数据库与 RLS

| 项目 | 结果 |
| --- | --- |
| 迁移记录 | `202607240001`、`202607240002` |
| 运行时角色 | `futures_runtime` 为 `NOSUPERUSER`、`NOBYPASSRLS`、`CANLOGIN` |
| RLS 状态 | `audit_logs` 启用并强制 RLS |
| Workspace 读取隔离 | Workspace A context 下 `visible_w1=1`、`visible_w2=0` |
| Workspace 写入隔离 | Workspace A context 写入 Workspace B 审计行被 RLS 拒绝 |

### 认证、Cookie、CSRF 与 Session

| 项目 | 结果 |
| --- | --- |
| 登录 | HTTP 200 |
| Cookie | dev 环境 `Secure=false`；包含 `HttpOnly`、`SameSite=Lax`、`Path=/`、`Max-Age=604800` |
| `me` | HTTP 200 |
| 当前 Workspace | 返回测试用户个人 Workspace，ID 与种子一致 |
| CSRF 获取 | HTTP 200，返回 token |
| 无 CSRF logout | HTTP 403 |
| 带 CSRF logout | HTTP 200 |
| logout 后 `me` | HTTP 401 |
| 并发 Session 上限 | 第 6 次登录后最旧 Session HTTP 401，当前活跃 Session 数为 5 |
| 跨用户 Session 撤销 | 非 admin 用户删除另一用户 Session 返回 HTTP 403 |
| 登录失败审计 | 10 次失败写入 `failure` 审计，第 11 次 429 写入 `denied` 审计 |

### Bootstrap 与生产安全防线

| 项目 | 结果 |
| --- | --- |
| bootstrap token 文件 | 初始化成功后宿主文件已删除 |
| 数据库 secret 文件 | `root:root`，权限 `0400` |
| 只读 secrets 挂载 | Compose 使用 secrets 目录只读挂载，token 删除后仍可 recreate/restart |
| 生产不安全配置 | `APP_ENV=production` 且 `AUTH_COOKIE_SECURE=false` 时启动前拒绝 |
| Argon2id 参数 | memory=64MiB、iterations=3、parallelism=1、version=1 |
| Argon2id 基准 | 本地 median 79.1ms；futures VPS median 136.2ms |

## 已关闭问题

| ID | 等级 | 问题 | 处理 |
| --- | --- | --- | --- |
| `P2-EVAL-LOGIN-AUDIT-001` | HIGH | Rate-limited login attempts 未写审计 | 已修复并在 VPS 证明 429 对应 `auth.login` / `denied` 审计 |

## 非阻塞记录

| ID | 等级 | 记录 | 后续 |
| --- | --- | --- | --- |
| `P2-EVAL-BOOTSTRAP-001` | MEDIUM | API 对只读挂载的 bootstrap token 文件执行删除可能失败；当前 VPS 已由部署流程删除宿主文件 | 保持为部署/运维步骤：初始化成功后立即删除宿主 token 文件 |
| `P2-EVAL-BUILD-001` | LOW | Vite build 存在大 chunk warning | 后续前端路由和页面增长后再做 code splitting |
| `P2-EVAL-GITSHA-001` | LOW | 当前 `/api/v1/version` 仍返回 `git_sha=local` | 后续 CI/镜像构建注入真实 Git SHA |
| `P2-EVAL-GRAPHIFY-001` | LOW | graphify 存在 34 条跨批次语义悬空边 | 作为非阻塞工具限制记录；最终仍以原始文档、Git、测试和 VPS 实证为准 |

## 结论

Phase 2 的用户初始化、个人 Workspace 隔离、认证与权限基础满足当前计划和用户确认参数。允许提交 Phase 2 收口变更。

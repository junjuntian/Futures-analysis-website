# futures VPS Phase 1 部署记录

## 状态

已完成 Phase 1 真实环境部署验证。

验证时间：2026-07-24 21:48 +08:00。

本次部署只包含 Phase 1 工程基础：PostgreSQL、Rust API、Rust Worker、Vue/Vite 前端开发服务和 Nginx。未实现后续阶段的业务功能。

## VPS 基础状态

| 项目 | 结果 |
| --- | --- |
| SSH 别名 | `futures` |
| 主机名 | `localhost` |
| 系统 | Ubuntu 26.04 LTS |
| 内核 | Linux `7.0.0-22-generic`，x86_64 |
| 根分区 | 25G，总用量部署后约 9.5G，约 14G 可用 |
| 内存 | 约 956MiB |
| Swap | 原系统约 495MiB；新增项目 swapfile 2GiB：`/var/lib/futures-platform/swapfile` |
| 对外监听端口 | SSH 22、本项目 HTTP 8088 |

## 已安装软件

| 软件 | 版本 |
| --- | --- |
| Docker | Docker version 29.1.3, build 29.1.3-0ubuntu4.1 |
| Docker Compose | Docker Compose version 2.40.3+ds1-0ubuntu1 |

Docker/Compose 通过 Ubuntu 26.04 系统仓库安装。

## 部署目录

| 用途 | 路径 | 说明 |
| --- | --- | --- |
| 项目部署目录 | `/opt/futures-platform` | 由本地 Git 源码归档上传生成 |
| 秘密目录 | `/etc/futures-platform/secrets` | `root:root`，不进入 Git |
| 主密钥文件 | `/etc/futures-platform/secrets/master-key-v1` | `root:root`，权限 `0400` |
| 数据目录 | `/var/lib/futures-platform` | 包含项目 swapfile |
| 备份目录 | `/var/backups/futures-platform` | 保存部署前/修复前项目备份 |

远端 `.env` 位于 `/opt/futures-platform/.env`，权限 `0600`，内容不写入 Git、文档或日志。

## 部署包

| 项目 | 结果 |
| --- | --- |
| 本地源 | Git HEAD 派生部署包 |
| 上传方式 | SFTP 上传压缩包到 `/tmp/futures-platform-deploy-src.zip` |
| SHA-256 | `5D9A8F18488CFF1B78EC5034B815FB1483DEA468917A4F343AC11B20E7845E31` |
| 远端校验 | 通过 |

## 容器与服务

| 服务 | 容器 | 镜像 | 状态 | 端口 |
| --- | --- | --- | --- | --- |
| PostgreSQL | `futures-analysis-platform-postgres-1` | `postgres:17.6-alpine` | healthy | 仅 Docker 网络内 `5432/tcp` |
| API | `futures-analysis-platform-api-1` | `futures-analysis-platform-api` | healthy | 仅 Docker 网络内 `8080/tcp` |
| Worker | `futures-analysis-platform-worker-1` | `futures-analysis-platform-worker` | running | 不暴露端口 |
| Frontend | `futures-analysis-platform-frontend-1` | `node:24.18.0-bookworm-slim` | running | 仅 Docker 网络内 `5173/tcp` |
| Nginx | `futures-analysis-platform-nginx-1` | `nginx:1.29.4-alpine` | running | `0.0.0.0:8088->80/tcp` |

访问地址：

```text
http://172.238.11.174:8088
```

## 数据库迁移状态

已执行：

| version | description |
| --- | --- |
| `202607240001` | `phase 1 foundation` |

验证结果：

- `schema_versions` 表存在。
- `app.current_workspace_id()` 函数存在。
- 当前无正式业务表。

## 健康检查结果

| 检查项 | 结果 |
| --- | --- |
| `GET /api/v1/health/live` | HTTP 200，`status=ok` |
| `GET /api/v1/health/ready` | HTTP 200，`status=ready` |
| `GET /api/v1/version` | HTTP 200，`name=futures-analysis-platform`，`version=0.1.0` |
| 前端页面 `/` | HTTP 200 |
| Nginx `/api/` 代理 | 通过 API 健康检查验证 |
| Worker | 容器运行，日志显示已连接数据库，数据库 URL 已脱敏 |
| 容器健康状态 | API/PostgreSQL healthy，其余基础容器 running |
| 日志秘密扫描 | 未发现 `POSTGRES_PASSWORD`、`BOOTSTRAP_TOKEN`、主密钥、明文 `DATABASE_URL` 或 URL 内密码 |

`checked_at` 已验证为 RFC3339 字符串，例如：

```json
{"status":"ready","checked_at":"2026-07-24T13:47:52.306460947Z"}
```

## 停止、启动和重新部署测试

| 操作 | 结果 |
| --- | --- |
| `docker compose --profile dev restart api worker nginx` | 通过，ready 检查成功 |
| `docker compose --profile dev stop api worker nginx frontend` 后 `start` | 通过，API ready 与前端页面均成功 |
| `docker compose --profile dev up -d --no-build` | 通过，服务保持可用 |

## 部署过程中发现并修复的问题

| 等级 | 问题 | 修复 |
| --- | --- | --- |
| HIGH | `frontend` 服务将项目目录只读挂载到 `/app`，同时挂载 `/app/node_modules` named volume，Docker 无法在只读父目录创建挂载点 | 将前端开发服务的项目挂载改为可写，以允许容器内安装依赖到 named volume |
| HIGH | 远端随机数据库密码最初使用 base64，写入 `DATABASE_URL` 后可能因 `/`、`+` 等字符导致 URL 解析失败 | 远端 `.env` 改用 hex 随机值，并备份后重建空 PostgreSQL volume |
| MEDIUM | `checked_at` 初始序列化为数组，与 OpenAPI 字符串示例不一致 | 启用 `time` 的 `serde-well-known`，字段使用 RFC3339 序列化 |
| MEDIUM | VPS 内存较小，Rust release 构建存在 OOM 风险 | 创建项目 swapfile `/var/lib/futures-platform/swapfile` 并启用 |

## 备份记录

| 备份 | 路径 | 说明 |
| --- | --- | --- |
| 数据库 volume 修复前备份 | `/var/backups/futures-platform/pre-env-fix-20260724T133322Z/postgres_data.tar.gz` | 修复 `.env` URL-safe 密码前创建；当时无业务数据 |

## 安全说明

- 未在文档、Git、普通日志或最终回复中记录远端 `.env`、数据库密码、`BOOTSTRAP_TOKEN` 或主密钥明文。
- 数据库端口未映射到宿主公网。
- 主密钥文件由 `root` 拥有，权限 `0400`。
- API 日志中数据库 URL 显示为 `[redacted]`。

## 当前限制

- 当前部署使用 Phase 1 开发型前端服务：`pnpm --dir frontend dev`，不是生产静态资源构建。
- `git_sha` 当前返回 `local`，后续生产镜像应通过构建参数注入实际提交号。
- 本机无 Docker，无法在本机执行 `docker compose config`；该校验已在 `futures` VPS 通过。

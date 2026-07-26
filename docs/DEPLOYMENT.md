# 部署说明

## 部署边界

- 本地 Git 仓库是唯一源码源头。
- VPS 上不得手工编辑业务源码。
- GitHub Actions / Codex Cloud 负责无生产数据的编译、测试和辅助审查；权威生产
  镜像由 GitHub Actions 构建并发布到 GHCR。
- `futures` VPS 不再进行常规 Rust 或前端编译，只负责数据库备份、镜像拉取、
  迁移、真实 PostgreSQL/RLS/文件持久化和最终 E2E 验收。
- 部署目录建议：`/opt/futures-platform`。
- 秘密目录建议：`/etc/futures-platform/secrets`。
- 数据目录建议：`/var/lib/futures-platform`。

## 本地启动

```powershell
Copy-Item .env.example .env
docker compose --profile dev up --build
```

本地默认入口：

```text
http://localhost:8088
```

## 唯一生产部署模式：GHCR 镜像

`.github/workflows/container-images.yml` 为 API、Worker 和前端生成
`linux/amd64` 镜像。镜像名固定为小写：

- `ghcr.io/junjuntian/futures-analysis-website-api`
- `ghcr.io/junjuntian/futures-analysis-website-worker`
- `ghcr.io/junjuntian/futures-analysis-website-frontend`

部署必须使用 `sha-<完整 Git SHA>` 标签或完整 digest，不以 `latest`
作为唯一或实际部署依据。API 镜像在 Rust 编译时注入同一 `GIT_SHA`，因此
`/api/v1/version` 可与部署镜像对应。

生产覆盖文件要求 Docker Compose 2.24.4 或更高版本，以支持 `!reset`；
当前 `futures` VPS 已记录的 Compose 2.40.3 满足要求。

### 切换门禁

本标准已经确认，但当前部署方式暂不立即切换。必须先确认：

1. CI 与 GHCR 工作流在目标提交上实际成功。
2. 三个 `linux/amd64` 镜像均存在 SHA 标签和 digest，API 版本返回真实 Git SHA。
3. VPS 的只读 GHCR 拉取凭据可登录、拉取且未写入仓库、命令历史或普通 `.env`。
4. 生产 Compose 渲染结果只引用该次发布记录中的 SHA 标签或 digest。
5. 数据库备份与恢复演练路径已验证。

全部满足并经用户确认后，VPS 才可执行：

```bash
export IMAGE_TAG=sha-<完整 Git SHA>
docker compose -f docker-compose.yml -f docker-compose.production.yml pull
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d
```

### 标准部署步骤

1. 核对目标 Git SHA、CI 结果、三个镜像 digest 和发布清单。
2. 以只读 GHCR 凭据登录；凭据不得出现在命令行参数、普通日志或环境文件。
3. 在 `/etc/futures-platform/secrets/postgres-password` 提供 PostgreSQL
   密码文件，并继续保留现有数据库 URL、bootstrap token、幂等 pepper
   等只读秘密文件。
4. 在任何生产迁移或容器切换前备份数据库，记录备份路径、校验值和恢复点；
   同时记录当前稳定的三个镜像 digest。
5. 执行 Compose config，确认没有 `build:`、`latest`、明文秘密或意外端口。
6. 执行 `docker pull`，不得在 VPS 运行 Cargo、pnpm 或 Docker 源码构建。
7. 按迁移顺序使用受控迁移身份执行数据库迁移，并核验 `schema_versions`；
   应用运行时身份不得获得迁移所有者权限。
8. 使用已拉取镜像启动服务，核验 `/api/v1/version`、健康检查和镜像 digest。
9. 执行真实数据库、RLS、文件持久化、重启恢复和完整 E2E；全部通过后才把
   本次 digest 标记为稳定版本。

### 部署回滚

- 发布清单必须保存“当前候选”和“上一稳定”三个镜像的完整 digest。
- 应用或 E2E 失败时停止继续发布，把生产 Compose/部署清单恢复到上一稳定
  digest，执行 `docker pull` 和 `docker compose up -d`，再复跑健康与 E2E。
- 数据库迁移失败或新旧镜像不兼容时，不得只回滚容器掩盖 schema 问题；按迁移
  的前滚修复方案处理，或从部署前备份恢复数据库后再启动上一稳定 digest。
- 回滚后保留失败版本、迁移、日志和证据用于审计，不在 VPS 修改源码“热修”。

## 主密钥挂载

futures VPS 主密钥文件：

```text
/etc/futures-platform/secrets/master-key-v1
```

要求：

- `root` 所有。
- 权限 `0400`。
- 只读挂载给需要解密的容器。
- 不进入 Git、Docker 镜像、PostgreSQL、日志或最终回复。
- 不进入普通 `.env` 文件或构建参数。

## 恢复与轮换

1. 新建主密钥版本文件，例如 `master-key-v2`。
2. 使用受控维护任务重新包裹 DEK。
3. 验证旧密文可由新版本解密。
4. 将 `key_version_metadata` 中旧版本标记为 retired。
5. 数据库备份与主密钥恢复副本分开保存。

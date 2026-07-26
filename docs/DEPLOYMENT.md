# 部署说明

## 部署边界

- 本地 Git 仓库是唯一源码源头。
- VPS 上不得手工编辑业务源码。
- GitHub Actions / Codex Cloud 负责无生产数据的编译、测试、镜像构建和辅助审查。
- `futures` VPS 负责迁移、真实 PostgreSQL、RLS、文件持久化和最终 E2E 验收。
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

## GHCR 生产镜像模式

`.github/workflows/container-images.yml` 为 API、Worker 和前端生成
`linux/amd64` 镜像。镜像名固定为小写：

- `ghcr.io/junjuntian/futures-analysis-website-api`
- `ghcr.io/junjuntian/futures-analysis-website-worker`
- `ghcr.io/junjuntian/futures-analysis-website-frontend`

部署必须使用 `sha-<完整 Git SHA>` 标签或记录的 digest，不以 `latest`
作为依据。API 镜像在 Rust 编译时注入同一 `GIT_SHA`，因此
`/api/v1/version` 可与部署镜像对应。

生产覆盖文件要求 Docker Compose 2.24.4 或更高版本，以支持 `!reset`；
当前 `futures` VPS 已记录的 Compose 2.40.3 满足要求。待 GitHub Actions
镜像构建成功且用户提供只读 GHCR 拉取凭据后，VPS 才可执行：

```bash
export IMAGE_TAG=sha-<完整 Git SHA>
docker compose -f docker-compose.yml -f docker-compose.production.yml pull
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d
```

当前不得切换 `futures` VPS。首次切换前必须：

1. 核对三个镜像的 SHA 标签和 digest。
2. 以只读 GHCR 凭据执行 `docker login ghcr.io`，凭据不得写入仓库或命令历史。
3. 在 `/etc/futures-platform/secrets/postgres-password` 提供 PostgreSQL
   密码文件，并继续保留现有数据库 URL、bootstrap token、幂等 pepper
   等只读秘密文件。
4. 备份数据库、对象存储和当前部署目录。
5. 先执行 Compose config，再 pull/up、迁移、健康检查、RLS 和完整 E2E。

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

## 恢复与轮换

1. 新建主密钥版本文件，例如 `master-key-v2`。
2. 使用受控维护任务重新包裹 DEK。
3. 验证旧密文可由新版本解密。
4. 将 `key_version_metadata` 中旧版本标记为 retired。
5. 数据库备份与主密钥恢复副本分开保存。

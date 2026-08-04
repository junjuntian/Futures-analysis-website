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

`.github/workflows/container-images.yml` 为 API、Worker、前端和 Collector 生成
`linux/amd64` 镜像。镜像名固定为小写：

- `ghcr.io/junjuntian/futures-analysis-website-api`
- `ghcr.io/junjuntian/futures-analysis-website-worker`
- `ghcr.io/junjuntian/futures-analysis-website-frontend`
- `ghcr.io/junjuntian/futures-analysis-website-collector`

部署必须使用 `sha-<完整 Git SHA>` 标签或完整 digest，不以 `latest`
作为唯一或实际部署依据。API 镜像在 Rust 编译时注入同一 `GIT_SHA`，因此
`/api/v1/version` 可与部署镜像对应。

生产覆盖文件要求 Docker Compose 2.24.4 或更高版本，以支持 `!reset`；
当前 `futures` VPS 已记录的 Compose 2.40.3 满足要求。

### 切换门禁

该标准已经由 Phase 3D 和 Phase 4A 实际运行。每次后续部署仍必须重新确认：

1. CI 与 GHCR 工作流在目标提交上实际成功。
2. 四个 `linux/amd64` 镜像均存在 SHA 标签和 digest，API 版本返回真实 Git SHA。
3. VPS 的只读 GHCR 拉取凭据可登录、拉取且未写入仓库、命令历史或普通 `.env`。
4. 生产 Compose 渲染结果只引用该次发布记录中的 SHA 标签或 digest。
5. 数据库备份与恢复演练路径已验证。

全部满足并在授权部署单内确认后，VPS 才可执行：

```bash
export IMAGE_TAG=sha-<完整 Git SHA>
docker compose -f docker-compose.yml -f docker-compose.production.yml pull
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d
```

### 标准部署步骤

1. 核对目标 Git SHA、CI 结果、四个镜像 digest 和发布清单。
2. 以只读 GHCR 凭据登录；凭据不得出现在命令行参数、普通日志或环境文件。
3. 在 `/etc/futures-platform/secrets/postgres-password` 提供 PostgreSQL
   密码文件，并继续保留现有数据库 URL、幂等 pepper、主密钥等只读秘密文件；
   按 DEC-026 保持 bootstrap token absent，不得在部署中重建。
4. 在任何生产迁移或容器切换前备份数据库，记录备份路径、校验值和恢复点；
   同时记录当前稳定的四个镜像 digest。
5. 执行 Compose config，确认没有 `build:`、`latest`、明文秘密或意外端口。
6. 执行 `docker pull`，不得在 VPS 运行 Cargo、pnpm 或 Docker 源码构建。
7. 按迁移顺序使用受控迁移身份执行数据库迁移，并核验 `schema_versions`；
   应用运行时身份不得获得迁移所有者权限。
8. 使用已拉取镜像启动服务，核验 `/api/v1/version`、健康检查和镜像 digest。
9. 执行真实数据库、RLS、文件持久化、重启恢复和完整 E2E；全部通过后才把
   本次 digest 标记为稳定版本。

### 部署回滚

- 发布清单必须保存“当前候选”和“上一稳定”四个镜像的完整 digest。
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

## Phase 4A Collector 生产部署实证（2026-08-03）

- 候选：`944a4defe578d5922b9f1ea83f951ddbd6fb005e`。
- CI Run：`30753685223`，success；包含 Rust、Python collector、前端、Compose 与四镜像构建门禁。
- Container images Run：`30753724067`，success。
- Deploy Run：`30754021926`，success；`PHASE4A_E2E_PASS`、`DEPLOYMENT_PASS`。
- 运行镜像：
  - API：`sha256:3ee25c7fd40c9f0e8c95caf8c3d068b8080a8d03e4fef29724c06c75e060abda`
  - Worker：`sha256:960173e949be5c07c6d1d71c64bd4ed5ca8ade8739b85ed27447e9e7c8d414e3`
  - Frontend：`sha256:deaa22ce164f7697e5319bbcc926ccf7321122cceee97ed6d9d838e244582875`
  - Collector：`sha256:bcb8d75db3a94be6280438e79fdf9ef7b5b0cb26009f05db2cfcef85d0d5ab7d`
- 已执行迁移：`202608020001_phase_4a_collection_schema.sql`、`202608020002_dce_fallback_source.sql`；部署报告同时核验既有 `202607260001`、`202607260002`。
- Collector 为一次性 `docker compose run --rm` 服务，`mem_limit: 512m`；三次 E2E 运行的 cgroup 实测最高峰值为 `130641920` bytes。
- host cron 已安装为工作日 17:30、21:30 两次运行 `/usr/local/sbin/run-futures-collector`；脚本使用 `/run/lock/futures-collector.lock` 与非阻塞 `flock` 防止重叠。
- 专用 analyst 服务账号由受控管理流程创建；凭据仅保存在 `/etc/futures-platform/secrets/collector-credentials`，部署核验 owner/mode 为 `root:root`/`0400` 并只读挂载。本文和部署日志不记录凭据内容。
- 真实验收日期为 `2026-07-30`：五交易所目录、日历、行情、席位批次成功；DCE 官方失败后仅 DCE 激活 `akshare_sina_dce_fallback`，其正式行情和席位来源指向真实聚合源；其他四家保持官方直连。
- 正式 `market_prices`、`seat_positions` 均大于 0，业务唯一键重复为 0；完整同日重跑后行数不变。故障注入时 DCE 行情批次 `failed`，其余四家 `succeeded` 且正式行情行数不变。
- RLS、批次/记录/正式事实表来源链、目录自动建档、手动批次整行指纹及用户稳定身份字段指纹全部通过。服务账号登录允许且仅允许更新 `last_login_at`/`updated_at` 登录元数据。
- 部署前基线为 144 个手动批次、25 个自动批次、32 个用户；手动批次未删除或篡改。Phase 3C/3D 生产 E2E 未重跑，避免制造新的手动测试批次。
- 临时 GHCR 登录配置清理通过；collector 日志的密码、Cookie、CSRF、Authorization 与凭据路径模式扫描无命中。

## Deploy self-hosted runner 资源限额（2026-08-04）

- 仓库级 runner 仅承接 `deploy-futures`，标签为 `futures-vps`；CI 与
  container-images 继续由 GitHub-hosted runner 编译，不把编译负载转移到 1 GB VPS。
- systemd unit 为
  `actions.runner.junjuntian-Futures-analysis-website.futures-vps.service`，资源 drop-in
  为同名 `.service.d/limits.conf`。
- 因 256 MiB 限额在既有部署验收中触顶并发生 cgroup 限流，已将
  `MemoryMax` 调整为 384 MiB。执行 `systemctl daemon-reload` 并在 runner 空闲时
  重启服务后，实态为 `active/running`，`MemoryMax=402653184` bytes。
- 本次仅调整 runner 服务资源上限，没有触发部署、迁移、E2E 或生产数据修改。

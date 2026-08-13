# 部署说明

## 部署边界

- 本地 Git 仓库是唯一源码源头。
- VPS 上不得手工编辑业务源码。
- GitHub Actions / Codex Cloud 负责无生产数据的编译、测试和辅助审查；权威生产
  镜像由 GitHub Actions 构建并发布到 GHCR。
- `futures` VPS 的 4 GiB 资源承载仓库级 self-hosted runner；CI 与镜像构建只可
  由受控 Actions 工作流在 2.5 GiB 总峰值护栏内执行。部署步骤不得手工运行
  Cargo、pnpm 或 Docker 源码构建，仍只拉取已发布镜像并执行备份、迁移和 E2E。
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
6. 执行 `docker pull`；部署作业和人工操作不得在 VPS 运行 Cargo、pnpm 或 Docker
   源码构建。只有仓库 Actions 的 CI/container-images job 可按本节资源护栏构建。
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

## 生产为什么跑在 APP_ENV=acceptance（2026-08-12 记录，知情取舍）

独立审查（FULL_AUDIT_20260812 HIGH-01）指出生产 API 长期运行在
`APP_ENV=acceptance`，production 级守卫（强制 HTTPS origin、Secure cookie、
禁环境变量 BOOTSTRAP_TOKEN、强制 DATABASE_URL_FILE）没有生效。**这是现状约束的
结果，不是遗漏**：站点按运营者拍板暂不配 TLS（明文 HTTP + IP 端口访问，等全站
完工后自己加域名再上 TLS），而 Rust 的 production 守卫会强制 HTTPS 与 Secure
cookie——现在切 production，登录当场瘫掉。单人自用面板，风险敞口有限。

**TLS 上线时必须一并做**：release overlay 把 `APP_ENV` 切回 production、
`AUTH_COOKIE_SECURE=true`、`PUBLIC_ORIGIN` 换 https 域名，并重新验证
ready/version 与登录。更彻底的解法（把验收开关与安全等级解耦）等那时一起做，
现在解耦只有成本没有收益。

## 采集账号密码轮换（2026-08-12 实证）

凭据文件 `/etc/futures-platform/secrets/collector-credentials`（root:root 0400，JSON）
是采集器登录用的唯一事实源。轮换 = 换文件里的 `password`，再让库向文件收敛：

```bash
# 全程在服务器上做，新密码不出这台机器。
set -euo pipefail; umask 077
. /var/lib/futures-platform/deployments/stable.env
export IMAGE_TAG="sha-${previous_git_sha}"
C=(docker compose -f "$previous_release_dir/docker-compose.yml"    -f "$previous_release_dir/docker-compose.production.yml"    -f "$previous_release_dir/docker-compose.release.yml")
FILE=/etc/futures-platform/secrets/collector-credentials
new_password=$(openssl rand -base64 48 | tr -d '
' | tr '+/' '-_')
tmp=$(mktemp /etc/futures-platform/secrets/.collector.XXXXXX)
jq -c --arg p "$new_password" '.password = $p' "$FILE" > "$tmp"
unset new_password
chown root:root "$tmp"; chmod 400 "$tmp"; mv "$tmp" "$FILE"
"${C[@]}" run --rm --no-deps api /app/api --provision-collector-account
```

`--provision-collector-account` 自 2026-08-12 起是收敛式的：账号已存在且密码与文件
不同 → 更新哈希并把该用户现存会话标记 `revoke_reason='password_rotated'`；停用的
账号仍然报错（复活是管理决定，不是采集配置）。在那之前它是只创建，文件与库不一致
直接报错——轮换无路可走。

两个踩过的坑：

- **换文件与跑收敛之间不要停。** 文件已换、收敛失败的中间态里采集器登不进去。
  第一次轮换正是卡在这里（收敛命令里有句 `delete from sessions`，futures_runtime
  对 sessions 没有 delete 权限——迁移 202607240002 有意只给 select/insert/update，
  会话史是审计素材，作废会话一律 update revoked_at）。**不要用回退文件的方式脱困**：
  需要轮换多半意味着旧密码已经泄露，回退等于把泄露的密码放回生产。修收敛命令，
  再往前走。
- 验证登录用 collector 镜像里的 python 读文件直接 POST `/api/v1/auth/login`，
  只打印状态码，密码不进 shell 历史也不进日志。

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
- host cron 已安装为工作日 09:30、13:30 **UTC** 两次运行 `/usr/local/sbin/run-futures-collector`（= 北京 17:30、21:30）；脚本使用 `/run/lock/futures-collector.lock` 与非阻塞 `flock` 防止重叠。（本条最初写作「17:30、21:30」而 crontab 里也真的填了 17:30——cron 按 UTC 解释，实际在北京时间凌晨跑，采到的是空的。2026-08-11 改为 UTC 表述并修正 crontab。）
- 专用 analyst 服务账号由受控管理流程创建；凭据仅保存在 `/etc/futures-platform/secrets/collector-credentials`，部署核验 owner/mode 为 `root:root`/`0400` 并只读挂载。本文和部署日志不记录凭据内容。
- 真实验收日期为 `2026-07-30`：五交易所目录、日历、行情、席位批次成功；DCE 官方失败后仅 DCE 激活 `akshare_sina_dce_fallback`，其正式行情和席位来源指向真实聚合源；其他四家保持官方直连。（该记录描述的是 2026-07-30 当天的实况。`DEC-045` 已于 2026-08-10 移除新浪兜底，大商所改由东方财富承担行情、目录与席位，此后的批次来源应指向 `eastmoney_dce_market` 与 `eastmoney_seats_fallback`。）
- 正式 `market_prices`、`seat_positions` 均大于 0，业务唯一键重复为 0；完整同日重跑后行数不变。故障注入时 DCE 行情批次 `failed`，其余四家 `succeeded` 且正式行情行数不变。
- RLS、批次/记录/正式事实表来源链、目录自动建档、手动批次整行指纹及用户稳定身份字段指纹全部通过。服务账号登录允许且仅允许更新 `last_login_at`/`updated_at` 登录元数据。
- 部署前基线为 144 个手动批次、25 个自动批次、32 个用户；手动批次未删除或篡改。Phase 3C/3D 生产 E2E 未重跑，避免制造新的手动测试批次。
- 临时 GHCR 登录配置清理通过；collector 日志的密码、Cookie、CSRF、Authorization 与凭据路径模式扫描无命中。

## Self-hosted runner 资源限额（2026-08-05）

- VPS 已由用户升配至 4 GiB；仓库级 runner 标签为 `futures-vps`，承接 CI、
  container-images 与 deploy-futures。禁止在 Actions 外直接编译业务源码。
- systemd unit 为
  `actions.runner.junjuntian-Futures-analysis-website.futures-vps.service`，资源 drop-in
  为同名 `.service.d/limits.conf`。
- runner `MemoryMax=2500M`（实态 `2621440000` bytes）；Cargo 使用 2 jobs 且关闭
  incremental，Node heap 限 768 MiB，CI PostgreSQL 限 384 MiB；BuildKit 单 job
  串行运行，memory/memory-swap 均限 2 GiB。runner 已加入 Docker 组；该组等价于
  root 权限边界，变更经用户明确授权，不得扩散至其他账号。
- 候选 `e627ab8` 的完整 CI 期间 runner cgroup 达到上限但 `oom=0`、`oom_kill=0`；
  生产五容器与 PostgreSQL 同期保持健康。构建峰值必须继续保持不超过 2.5 GiB。

## Phase 4A HIGH-03 终验候选部署实证（2026-08-05）

- 业务修复：`23e679db3b7fa3a384e764235e6ea3066d18766f`；发布候选：
  `e627ab8c3b797cc77f872a9c02439c1dfca0d4eb`。
- CI Run `30969365344` success；Container images Run `30970280360` success；
  Deploy Run `30971024520` success。
- 运行镜像：
  - API：`sha256:cb9145eca282dc72d0e916723e12ead471d4376aee85852a93a5ed8ece85f3de`
  - Worker：`sha256:597fe69d47b442ae2569d24ddb013a9e9ebf7a8b69de3e802b41032a3f5cb0af`
  - Frontend：`sha256:54c3c0e7f33eddb42356fd13cbf64772939b699ec1c40645d99930642ee90f36`
  - Collector：`sha256:e07b34a05316b620d9ddf68db57b054e9227665951bf51ad33245f55c1891e60`
- 最新 release 证据目录为
  `/var/lib/futures-platform/deployment-evidence/e627ab8c3b797cc77f872a9c02439c1dfca0d4eb-20260805T030140Z`；
  `/api/v1/version` 返回同一 Git SHA。
- Phase 4A E2E 覆盖授权矩阵、目录受控 upsert、全部正式投影回滚、HIGH-03 的
  exchange 被 calendar version 引用时预检拒绝与零变更、DCE 来源恢复和同日重放
  幂等，最终输出 `PHASE4A_E2E_PASS`。Collector 峰值 `179359744` bytes，低于
  512 MiB 限额。
- 部署后只读快照：手动批次 144、用户 32、`market_prices=9020`、
  `seat_positions=186083`，两类业务唯一键重复组均为 0；bootstrap token absent，
  collector 凭据仍为 `root:root`/`0400`。本节不记录任何秘密内容。
- 本次完成 Generator 发布链，不构成独立 Evaluator 的 Phase 4A PASS；不得据此合并
  main、打标签或启动 Phase 4B-2。

# 发布流程

**这份文档是给接手的会话看的操作手册。照着做就能把改动送上生产,不用去读三个
工作流的 YAML。**

配套两份:

- `ops/preflight-deploy.sh` —— 能机械查的前置条件全在里面,每次部署前跑。
- `docs/DEPLOY_PREFLIGHT.md` —— 脚本查不了、必须自己过一遍的东西,以及每条规则
  是被哪次失败教出来的。

---

## 零、先认清这条流水线的形状

改动不是"推上去就生效"。要走三步,**每一步都是独立触发的,不会自动接力**:

```text
本地改 → push → ① CI(自动)
                 ② 构建镜像(手工触发 container-images.yml)
                 ③ 部署(手工触发 deploy-futures.yml,把镜像 digest 喂进去)
```

②③ 都要人去点。只 push 不触发,生产一动不动——这是最常见的"我明明改了啊"。

生产机: 服务器别名 `qh`(172.238.18.206),站点 `https://shejimao.trade`。
2026-08-13 从 `futures`(172.238.11.174)迁过来;**老机已退役,实例于 2026-08-15
由运营者在 Linode 面板删除**(此前已停容器与 runner、cron 移至 /root/retired-cron、
关机)。

**因此现在没有可切换的热备机器**——出事只有两条退路,别指望第三条:

1. **异地备份**:每日整库 dump 推到 ssp(172.104.107.155:/root/futures-db-backup/latest.dump,
   只留最新一份,先 `pg_restore --list` 验可读再原子覆盖,脚本
   `deploy/collector/offsite-db-backup.sh`,cron 每日 15:40 UTC)。
   首次自动执行 2026-08-14T15:40Z 成功,234,407,468 字节。
2. **部署链回滚**:每次部署前在 qh 本机备份,失败自动拦下、线上仍是上一版。

**构建跑在 GitHub 托管 runner**,不再用自建 runner。仓库 2026-08-13 转为公开,
托管 runner 对公开仓库免费,每个作业各拿一台全新机器——不排队、不互相干扰,
也不存在"重启 runner 把作业杀掉"这回事。部署仍然 ssh 到 `qh` 执行。

---

## 一、四条铁律

1. **从建镜像到部署完成,不要推任何提交。**
   推了 HEAD 就变,部署被 `acceptance_sha` 守卫拒绝。**文档提交也算**——挡住过一
   次的就是一个只改 `docs/DECISIONS.md` 的提交。要改文档,等部署完。

2. **四个镜像必须齐。** 构建矩阵是 api / frontend / collector 三个(worker 已随
   导入通道摘除)。作业 `fail-fast: false`,少一个不会让整体变红——preflight 会
   逐个核对 `sha-<完整 SHA>` 标签存在,这一条别绕过。

3. **不要为了让部署过去而放宽守卫。** 每一条守卫都是被一次真实事故加上的。

4. **生产库禁止手改数据、禁用 RLS 或改约束来规避失败。** 失败就回滚或前滚修复。

---

## 二、标准流程

### 第 0 步:本地门禁

```bash
cd rust && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test
```

```bash
cd frontend && pnpm lint && pnpm test && pnpm build
```

### 第 1 步:提交并推送

分支是 `phase/05-spread-analytics`(`ops/preflight-deploy.sh` 里的 `BRANCH` 就是它,
换分支要同步改)。Conventional Commits。

推完等 CI 变绿:

```bash
gh run list --workflow=ci.yml --limit 1
```

### 第 2 步:构建镜像

```bash
gh workflow run container-images.yml --ref phase/05-spread-analytics
```

**这一步是按需构建的**,四个镜像各自判定重建还是复用(见第三节)。看判定结果:

```bash
gh run view <run-id> --log | grep -E "DECIDE image=|image-built-from"
```

耗时:全量重建约 10–17.5 分钟;四个全复用约 2 分钟;只改前端约 4 分钟。

### 第 3 步:前置检查

```bash
bash ops/preflight-deploy.sh
```

全绿才会打印出可直接执行的 dispatch 命令(digest、复用来源、验收日期都替你填好了)。
不绿就照它说的修,**不要跳过**。

它查五类:本地仓库状态、迁移、发布包完整性、镜像发布情况、runner 状态。
脚本查不了的那几类在 `docs/DEPLOY_PREFLIGHT.md`,同样必须过。

### 第 4 步:部署

把上一步打印的命令原样执行,或者:

```bash
bash ops/preflight-deploy.sh --dispatch
```

部署作业在 VPS 上会依次做:备份数据库 → 拉镜像并核验 digest 与 revision 标签 →
用受控身份跑迁移并核验 `schema_versions` → 起服务 → 跑生产验收(真实写库的
E2E)→ 全过才把这套 digest 登记为新的稳定版本。

任何一环失败,**发布被拦下,线上仍是上一版**。

### 第 5 步:部署后验证

```bash
gh run view <deploy-run-id> --log-failed   # 失败时
```

成功后至少确认三件事(前两件部署作业已经核过,第三件必须自己做):

- 新迁移出现在 `schema_versions`
- api `/version` 的 sha 与本次一致 —— **注意它是「镜像构建时」的 sha,不是本次发布的
  sha**。镜像复用是常态(见第三节),所以复用时这两个数**本来就不一样**,
  别拿它当发布是否生效的判据(2026-08-20 我就这么误判过一次)。
  要看本次发布的 sha,读服务器上的 `/var/lib/futures-platform/deployments/stable.env`。
- **改了 `engine/` 的话,确认信号 JSON 已经重算过**。机构资金那几个页面读的是
  **每日定时任务产出的静态 JSON**,不是接口实时算的:部署只换代码、不重算 JSON,
  于是「代码已上线、页面还是旧引擎算的数」——**而且看不出来**,页面照常显示。
  2026-08-20 DEC-096 上线后就这样过了一夜,运营者看到玻璃仍显示
  「进场 FG2609 / 现价 FG2701」那笔本该被平掉的持仓才发现。
  现在有两道保险(DEC-099),但**两道都要会自己查**:
  - 部署会自动重算(`ENGINE_REFRESH ok` 出现在部署日志里,失败非阻断);
  - 页面在指纹对不上时挂「这份信号是旧引擎算的」横幅。
  手工核:`ls -la /opt/futures-platform/smart-money/web/*.json` 的时间应当
  **晚于** `hog_money.py`;要补跑就 `/usr/local/sbin/run-smart-money`(幂等全量重放)。
- **构造一个真实请求打到改动过的端点,看它返回什么**——路由存在不等于它能用
  (401 只说明路由在)。改了前端就到 `https://shejimao.trade` 亲眼看一次
  (此处原写老机 `172.238.11.174:8088`,该机 2026-08-15 已退役删除,
  2026-08-16 更正)。

### 第 6 步:部署完更新 LATEST(有门禁盯着)

`docs/handoffs/LATEST.md` 里那行「**生产 = `<sha>`**」写的是**上一次成功部署的
SHA**,部署完之后用一个**纯文档提交**把它更新成刚部署的那个。那个提交**不单独
部署**,跟着下一次发布一起走。

**为什么不是「即将部署的那个」**:一个文档提交没法在自己被部署之前就写出自己的
SHA(自指)。2026-08-20 我按「即将部署」的写法做了两轮,每轮都留下一个新的过期,
才发现规则本身有问题。

`ops/preflight-deploy.sh` 有门禁核这条等式(DEC-100):
`LATEST 里的 SHA == 上一次成功部署的 SHA`,对不上就红,发不出去。
**加它的原因**:2026-08-20 一天部署七次漏更新一次,而漏掉的表现和引擎那个 bug
一模一样——**看起来一切正常,只是记录是旧的**。

---

## 三、镜像复用是常态,不是异常

构建工作流按各镜像的输入路径判定要不要重建:

| 镜像 | 输入路径 |
| --- | --- |
| api | `rust/` |
| frontend | `frontend/` |
| collector | `collector/` |

路径没变就把上一版镜像重打本次 sha 的标签(`docker buildx imagetools create`),
秒级完成,内容逐字节相同。

复用**不放宽任何守卫**,只是把「镜像来自本次 sha」细化成「镜像来自与本次 sha 在
其输入路径上逐字节一致的提交」,而且这条等价性在 deploy 的 runner 上用 git diff
实际核验过。

两个必须记住的点:

- **两边的路径集必须一致**:`container-images.yml` 的 `paths` ↔ `deploy-futures.yml`
  的 `image_paths`。改一边必须同步另一边——不一致的方向,要么白重建,要么放走旧代码。
- **复用会成链,申报的必须是「最初构建自」而不是「上一跳」。** A 复用自 B、B 又
  复用自 C 时,镜像里的 `revision` 标签始终是 C,而 deploy 侧核验的正是这个标签。
  2026-08-11 Run 31503399543 就是报了上一跳,标签核验对不上,部署被静默拦下。

---

## 四、部署失败了怎么办

1. **先确认生产没被改动**。验收失败会拦下发布,线上仍是上一版。
2. 看日志:`gh run view <id> --log-failed`。**真正的失败步骤往往在报错那步的上面**
   ——后面的报错常常只是收尾步骤的连带(比如 SSH 密钥已被清理)。
   裸 `test` 失败不打印任何东西,表现为"远程脚本十几秒无输出就退出",别被骗。
3. 修完**要从第 2 步重来**,因为 HEAD 变了。

## 五、回滚

1. 每次发布前保存上一稳定的四个镜像完整 digest(部署作业会记录)。
2. 应用/健康检查/E2E 失败时停止发布,把部署清单恢复到上一稳定 digest,重新
   pull/up 并复跑验收。
3. **迁移已经执行过的话**,先判断上一镜像是否兼容当前 schema;不兼容就用已批准的
   前滚修复,或从部署前备份恢复后再启动上一稳定 digest。
4. 回滚结果、所用 digest、数据库恢复点和验证证据写进发布记录。

---

## 六、生产上的定时任务

不走上面的流程,但发布会更新它们(脚本从发布目录读),改了要知道:

| 时间(UTC) | 北京 | 任务 |
| --- | --- | --- |
| 工作日 09:30 | 17:30 | `run-futures-collector` —— 采集 + 新浪大商所日更 + 装载进两张历史表 |
| 工作日 09:55 | 17:55 | `run-official-seats` —— 上期所/郑商所官方席位增量(带增减量) |
| 工作日 10:10 | 18:10 | `run-smart-money` —— 机构资金信号引擎(AU/AG) |
| 工作日 13:30 | 21:30 | `run-futures-collector` 补一轮 |
| 工作日 13:55 | 21:55 | `run-official-seats` 补一轮 |
| 工作日 14:10 | 22:10 | `run-smart-money` 补一轮 |
| 工作日 15:00 | 23:00 | `run-futures-spread-warm` —— 预热价差 |
| 每日 15:40 | 23:40 | `run-futures-offsite-backup` —— 整库备份推 ssp,只留最新一份 |

**顺序是硬要求**:引擎读的是席位表的增减量,跑在官方席位之前就会算在旧数据上。
三套 cron 文件都在 `deploy/collector/`,随发布包下发——2026-08-13 迁新机时才发现
后两套长期只存在于服务器上、靠手工安装,换机器没有任何东西保证它们会被带过去。

**cron 的时间是 UTC,不是北京时间。** 写成北京时间的话它按 UTC 解释,会在完全无关
的时刻跑。09:30/13:30 UTC = 北京 17:30/21:30。

采集脚本 `deploy/collector/run-collector.sh` 的顺序是硬要求:采集 → 大商所日更 →
装载。装载必须看得到刚落库的那一天,所以三步在同一个脚本里,不拆成三条 cron。

采集器写出 CSV,`load-*-direct.sql` 直接 upsert 进宽表(`DEC-049`,2026-08-13)。
在那之前这条路要经过导入通道的七层流水线,现在没有了。**采集失败只写
`.csv.failed` 标记,不写数据 CSV**——写一个只有表头的空文件会被装载脚本当成
"今天这个交易所一行都没有"而照单全收,把已在库的当日数据 upsert 成空值。

**采集部分失败不会中断后面两步**(大商所行情按 DEC-047 是已知采不到的,采集器每天
都非零退出),失败状态留到脚本末尾如实退出。这是 2026-08-11 修的:在那之前
`set -e` 让脚本死在采集那一行,**日更投影从来没有自动成功过**,数据停在
`market_prices` 进不了 `price_history`(`market_prices` 这张中转表已随 `DEC-049` 删除)。

---

## 七、约束

- **生产 VPS(`qh`)禁止直接修改源码、禁止现场编译。** 只 `docker pull` 已发布的镜像。
- 生产秘密不得进入 Git、镜像层、构建日志、build arg 或普通 `.env`。
- 云端(Codex Cloud 等)不得连接生产 VPS、生产数据库或读取生产秘密;
  它的测试结果不能替代第 5 步的生产验证。
- 每个镜像必须有 `sha-<完整 Git SHA>` 标签和完整 digest;`latest` 不能作为部署依据。
- `research/` 是研究脚本区,长期处于改动状态,前置检查有意不拦它。
  `engine/`(机构资金引擎)**随发布包下发并自动装到
  `/opt/futures-platform/smart-money/`**(2026-08-18 实证发布包与运行位置 md5
  一致)——旧文档里「引擎不走镜像链要手动 install」的说法已过时。改了引擎想立即
  生效,部署完成后手动跑一次 `run-smart-money` 即可,不必等晚间 cron。

---

## 八、部署后的手动重算(改了监控采集 SQL 必做)

改了 `deploy/collector/compute-spread-monitor.sql`(分档、圈定、新列等)的部署,
**完成后表里的存量数据还是旧口径**——日更 cron 只算最近 3 天,不会替你把历史刷新。

```bash
# 在 qh 上执行。脚本只从 stable.env 指向的发布目录取——
# /opt/futures-platform 是旧 git 检出,拿它的 SQL 重算过一次,把 45 天新列
# 全刷成 NULL(2026-08-18,PITFALLS 一)。
. /var/lib/futures-platform/deployments/stable.env
docker exec -i futures-analysis-platform-postgres-1 \
  psql -U futures_app -d futures_platform -v ON_ERROR_STOP=1 -v window_days=45 \
  < "$previous_release_dir/deploy/collector/compute-spread-monitor.sql"
```

- 窗口:日常改动 `window_days=45`(约 10-40 秒);动了**历年口径**(组合圈定、
  历年实例、回归率算法)用 `window_days=5000` 全量(约 20-40 分钟,anchor_row
  是大头;nohup + 日志轮询,ssh 长等待会超时但不杀进程)。
- 重算完必须核对:新列非 NULL(`count(pair_pos_hi20)` 等于行数)、最新日组合数
  符合预期、抽一行看分档判定。约束报错先看被拦的值是不是真实观测(PITFALLS 一)。

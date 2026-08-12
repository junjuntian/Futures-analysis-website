# 全项目独立审查报告（2026-08-12）

审查身份：独立 Evaluator，只审不改。仓库静态基线为 `phase/05-spread-analytics` 的 `af0e1ab`；生产证据均来自 `futures` VPS 的 `cat/ls/grep/docker inspect` 或 PostgreSQL 只读 `SELECT`（需要验证 RLS 时使用 `futures_runtime + set_config`）。未触发 workflow、部署、重启或任何生产写入，也未审查 `research/` 和生产 `/opt/futures-platform/smart-money/` 的内部逻辑。

结论：共确认 25 条 finding（HIGH 15、MEDIUM 9、LOW 1）。用户列出的 8 个已修问题均未原样重报；报告中的锁覆盖、来源断言、cron 覆盖和发布清单问题是同类的新实例。

## Findings

### [HIGH]-01 生产 API 长期运行在 acceptance 环境，production-only 安全守卫全部失效
- 位置:.github/workflows/deploy-futures.yml:601-615；docker-compose.production.yml:9-19；rust/apps/api/src/auth.rs:59-85；rust/crates/common/src/lib.rs:33-46
- 失败场景:release overlay 把 `APP_ENV=production` 覆盖为 `acceptance` 且成功后不切回 → `AUTH_COOKIE_SECURE=false`、HTTP origin、环境变量 bootstrap token 或数据库 URL 回退都不会触发生产拒绝 → 服务和 E2E 正常 PASS，但会在 HTTP 响应中发出不带 `Secure` 的 session cookie，且生产 secret-file 约束没有生效。
- 证据:workflow 明写 API/Worker `APP_ENV: acceptance`；生产只读 `docker inspect` 输出 `APP_ENV=acceptance`、`AUTH_COOKIE_SECURE=false`、`PUBLIC_ORIGIN=http://<VPS>:8088`。Rust 只在 `APP_ENV == production` 时强制 HTTPS、secure cookie、禁止 `BOOTSTRAP_TOKEN` 环境变量并要求 `DATABASE_URL_FILE`；`auth.rs:1421-1430` 仅在 `cookie_secure=true` 时追加 `Secure`。
- 建议:验收完成后以 production overlay 启动并重新验证 ready/version；更稳妥的是把验收功能开关与安全环境级别解耦，让 production 守卫在验收期间也始终开启。

### [HIGH]-02 部署锁未覆盖 official-seats 与 spread-warm 两个生产写者
- 位置:.github/workflows/deploy-futures.yml:310-315；deploy/collector/run-spread-warm.sh:5-20；engine/run-official-seats.sh:43-95
- 失败场景:13:55 official-seats 或 15:00 spread-warm 与部署的 owner 迁移/回滚重叠 → 两者不持 `/run/lock/futures-collector.lock`，DDL 仍会争锁，回滚的 `pg_terminate_backend` 仍会杀其连接；official-seats 的多条 SQL 没有总事务，还可留下价格已提交、席位尚未 upsert/delete 的半轮数据。
- 证据:部署只拿 collector lock；spread-warm 拿的是另一把 `/run/lock/futures-spread-warm.lock`，official wrapper 全文没有 `flock`。生产 cron 为 official 09:55/13:55 UTC、spread 15:00 UTC；只读日志显示 2026-08-12 spread 从 15:00:01 跑到 15:16:42，而最新部署从 14:58:29 开始、`deployed_at=15:00:15`，两者已经真实重叠。
- 建议:所有会写受迁移影响对象的作业统一使用同一维护锁或 PostgreSQL advisory lock，并固定锁顺序；official 的整轮灌库应有一个事务边界。

### [HIGH]-03 回滚不恢复 E2E 前已经覆盖的宿主机脚本与 cron
- 位置:.github/workflows/deploy-futures.yml:443-530；.github/workflows/deploy-futures.yml:811-863
- 失败场景:候选 wrapper、backfill Python、cron/logrotate 已覆盖宿主机 → Phase 4/5 随后失败 → 回滚只恢复数据库和 compose 镜像，宿主机仍是候选文件 → 下一次 cron 让新 wrapper 对旧 schema/旧 stable 状态运行，产生缺文件、SQL 不兼容或错误写入。
- 证据:`811-839` 在 E2E 前覆盖三份 `/usr/local/sbin` wrapper、`/opt/futures-platform/*.py`、collector cron 和 logrotate；`842-863` 才执行可失败的两套 E2E。`rollback_deployment()` 的 `443-530` 没有任何 host artifact 恢复；生产 `.superseded` 只覆盖部分 Python，不含 wrapper/cron。
- 建议:host 产物随 release 版本化并由 stable 指针选择；若仍复制到固定路径，则安装前完整快照，回滚时逐项恢复并校验 hash。

### [HIGH]-04 stable.env 提交后仍有失败窗口，回滚却不还原发布指针
- 位置:.github/workflows/deploy-futures.yml:543-551；.github/workflows/deploy-futures.yml:586-598；.github/workflows/deploy-futures.yml:889-937
- 失败场景:`stable.env` 已写成候选 → 随后的报告写入、复制、chmod、`cat` 或 SSH 断连失败 → EXIT trap 把数据库和服务回旧，但 stable 指针仍指候选 → 下次 collector/spread wrapper source 该文件，又启动未通过验收的 release。
- 证据:`889-902` 直接以 `> "$STATE_ROOT/stable.env"` 覆盖文件，之后 `904-937` 仍有多项受 `set -e` 约束的命令；rollback 只 source `PREVIOUS_STABLE` 来 `compose up`，从未把它复制回 stable。`run-collector.sh:5-10,41-47` 与 `run-spread-warm.sh:5-10,29-36` 都把 stable 文件当运行时真源。
- 建议:先完成报告与所有可失败校验，最后用临时文件加原子 `mv` 提交 stable；rollback 显式恢复 `PREVIOUS_STABLE`，并校验指针、运行镜像和版本一致。

### [HIGH]-05 标准 preflight 无条件关闭唯一真实 collector 端到端门禁
- 位置:ops/preflight-deploy.sh:226-237；.github/workflows/deploy-futures.yml:39-43；rust/tests/phase_4a_e2e.sh:174-190
- 失败场景:collector parser、来源路由或 wrapper 有单测未覆盖的生产集成错误 → 标准 preflight 仍传 `run_live_collection=false` → Phase 4 只检查生产已有行非空和少量 fingerprint 后提前 PASS → 坏 collector 直接上线，直到下一次 cron 才真实失败。
- 证据:preflight 没有任何 changed-path 判断，固定生成 `-f run_live_collection=false`；workflow 的默认值和说明反而是 true。false 分支在 `phase_4a_e2e.sh:179-189` 不启动 collector。生产最新 deployment report 也记录 `run_live_collection=false`。
- 建议:根据当前 stable 到候选的 diff，对 collector/Phase 4/运行 wrapper 相关路径强制 true；默认保持 true，false 必须是显式且留痕的豁免。

### [HIGH]-06 Phase 4 full gate 的 DCE 来源断言已经与当前 collector 漂移
- 位置:rust/tests/phase_4a_e2e.sh:246-263；rust/tests/phase_4a_e2e.sh:301-308；collector/src/futures_collector/sources.py:41-77；collector/src/futures_collector/sources.py:215-257；collector/src/futures_collector/runner.py:96-103
- 失败场景:把 `run_live_collection` 打开 → 当前 DCE 行情/目录使用 `eastmoney_dce_market`、席位使用 `eastmoney_seats_fallback` → E2E 仍只统计退役的 `akshare_dce_official`/`akshare_sina_dce_fallback`，并要求 market/seat 同源 → 第 251 行起的断言必失败，生产切换进入整库回滚。
- 证据:collector 常量和 runner 明确区分两个 Eastmoney 来源；E2E 的 source 集仍是旧的两个 akshare code。生产只读 SELECT 的近期 automatic batches 也只见 Eastmoney 新来源。preflight 只比对 cron 字符串，完全不比对来源契约。
- 建议:由 collector 导出唯一、按 dataset 区分的来源 manifest，E2E 直接消费；禁止在验收脚本再次手写 source code 集合。

### [HIGH]-07 前端镜像复用输入漏掉 Dockerfile 实际 COPY 的三个根清单
- 位置:frontend/Dockerfile:4-6；.github/workflows/container-images.yml:39-43；.github/workflows/container-images.yml:104-125；.github/workflows/deploy-futures.yml:131-147；ops/preflight-deploy.sh:35-42
- 失败场景:只更新根 `pnpm-lock.yaml`（例如传递依赖安全修复）、`package.json` 或 `pnpm-workspace.yaml` → container workflow 因只 diff `frontend/` 而复用旧镜像 → deploy 又用同一份缺漏路径判断“逐字节等价” → 部署报告成功，但生产镜像不含候选依赖。
- 证据:frontend Dockerfile 明确 `COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./`；构建矩阵的 frontend `paths` 和 deploy 的 `image_paths[frontend]` 都只有 `frontend/`。preflight 的 deployed dirty 正则也不包含这三个根文件。
- 建议:建立单一、可测试的每镜像完整输入 manifest，由 build、deploy、preflight 共用；frontend 至少加入三个根清单，并增加 lockfile-only 变更测试。

### [HIGH]-08 仓库追踪且生产执行的 engine 代码完全不在发布链
- 位置:engine/run-official-seats.sh:1-96；engine/run-smart-money.sh:1-56；.github/workflows/deploy-futures.yml:169-279；ops/preflight-deploy.sh:30-37；ops/preflight-deploy.sh:106-134
- 失败场景:提交 `engine/run-official-seats.sh` 或 `engine/run-smart-money.sh` 修复 → CI、preflight 和标准 deploy 全绿 → 发布包没有 engine，生产 cron 继续运行手工旧副本；反向的服务器手工修改也没有 deployment SHA 和回滚路径。
- 证据:`git ls-files engine` 包含两份生产 wrapper 和 `smart_money.py`；生产 cron 直接调用对应 `/usr/local/sbin` 文件，当前 wrapper hash 恰与仓库相同但仅靠手工同步。bundle 只列 collector、backfill、migrations、E2E；preflight 有意把 engine 排除。本条不重复报告用户已知的两个 cron 文件本身不在仓库。
- 建议:把 engine 定义成独立、版本化、带清单和回滚的运营发布单元，或明确移出本仓 source-of-truth；不能继续依赖无记录的手工同步。

### [HIGH]-09 rollback 在核心恢复失败后仍继续启动旧服务，且可能假报 PASS
- 位置:.github/workflows/deploy-futures.yml:443-529
- 失败场景:锁外写者在 terminate 后重连，使 `dropdb` 失败 → rollback 只把状态记为 1，仍继续 `createdb`、`pg_restore` 并启动旧镜像 → 旧 binary 对部分迁移/部分恢复的 schema 运行；或 `compose up -d` 返回 0 后 API 立即崩溃，rollback 仍可打印 PASS。
- 证据:`472-483` 对 drop/create/restore 的错误只累计 `rollback_status`，没有 fail-stop；`494-503` 的 previous stable 分支只有 `up -d`，没有数据库 fingerprint、旧 digest、health、version 或 ready 校验。PASS 只取决于这些命令的同步退出码。
- 建议:drop/create/restore 任一步失败都应保持业务服务停止并转人工恢复；只有备份、关键 schema、旧镜像 digest、ready/version 全部验证通过后才允许标记回滚成功。

### [HIGH]-10 品种汇总把部分未知的 change 算成确定值
- 位置:deploy/collector/compute-seat-totals.sql:41-43
- 失败场景:同一会员、品种、日期、榜别、来源有多个合约，其中一个 `change=NULL`、其余两个合计 `-458` → PostgreSQL `sum(change)` 忽略 NULL → 汇总行写成 `-458`，把不完整的部分和伪装成完整增减值，违反 `NULL != 0`。
- 证据:SQL 直接选择 `sum(s.change)`。生产以 `futures_runtime + set_config` 的只读事务检查近 10 天，发现 11 个 NULL/非 NULL 混合组；例如 `DCE|JD|2026-08-07|short|东吴期货|sanhe|null=1|known=2|sum=-458`，已存计算汇总同样为 `-458.00000000`。
- 建议:只有 `count(change)=count(*)` 时才求和，否则汇总 change 保持 NULL；增加混合 NULL/已知合约的行为测试。

### [HIGH]-11 canonical 导入回滚后历史宽表仍保留已撤销事实
- 位置:deploy/collector/project-history.sql:38-101；deploy/collector/project-history.sql:112-152；rust/crates/database/src/rollback_jobs.rs:332-372
- 失败场景:自动导入成功并投影到 `price_history`/`seat_history` → 用户执行 direct rollback，canonical `market_prices`/`seat_positions` 被删除 → 下一轮投影只遍历仍存在源行，没有 delete 或 invalidation 消费 → 已撤销价格/席位继续被 API、价差、监控和品种汇总使用。
- 证据:两段投影只有 `INSERT ... ON CONFLICT DO UPDATE`，没有删除；rollback 明确删除 canonical 表。生产只读查询确认近 10 天存在大量“可 direct rollback 且已投影”的真实批次，例如 2026-08-12 两个 akshare 行情批次投影 31/20 行、eastmoney 席位批次投影 1,020 行；任选其一回滚即可确定复现。当前已 rolled_back 批次碰巧未留下近 10 天陈行，不能证明路径安全。
- 建议:历史表保留 source record/import batch lineage，并由 rollback 同步清除；或每轮原子重建受管 `(workspace,date,source)` 切片并消费 `import_data_invalidations`。

### [HIGH]-12 新浪抓取部分乃至全部失败仍返回成功，upsert 会保留被撤回的旧行
- 位置:deploy/collector/sina-dce-daily.py:165-177；deploy/collector/sina-dce-daily.py:211-214；deploy/collector/load-dce-daily.sql:22-56；deploy/collector/run-collector.sh:78-95
- 失败场景:某合约请求异常或某历史 bar 被质量检查拒绝 → Python 只打印 FAIL/REJECT 并继续，最终固定返回 0 → loader 用不完整 CSV 做纯 upsert，不删除本轮 absent key → 上游已撤回或本轮判坏的旧价格继续留在历史图和价差；即使全部合约失败，DCE 子链也可成功退出。
- 证据:异常分支只有 `continue`，函数末尾无条件 `return 0`；loader 只有 insert/upsert，没有 staging 完整性阈值或按日期/source 替换切片。runner 在 Python 返回 0 时必定继续装载，并且最终退出状态不包含该子链的失败数。
- 建议:抓取端对零产出和低于完整性阈值返回非零；完整数据先进入 staging、校验后原子替换受管切片，禁止以部分文件直接 upsert 权威快照。

### [HIGH]-13 发布包中的历史 loader 仍会选择 UUID 最小的测试 Workspace
- 位置:backfill/load_history.py:3-8；backfill/load_history.py:121-125；backfill/load_history.py:150-181；backfill/README.md:9-24
- 失败场景:运营者运行已发布的 `load_history.py --what czce|shfe|sanhe|all` → 脚本以可绕 RLS 的 `futures_app` 连接，并执行 `select id from workspaces order by id limit 1` → 历史全写入 Phase 3C 测试租户，正式页面不更新；每约 5,000 行 commit，一旦发现目标错误也无法整次原子撤销。
- 证据:生产只读 SELECT 显示 UUID 最小空间是 `018f0000-0000-7000-8000-000000000311 | Phase 3C E2E 1`，唯一有 `market_prices` 的运营空间是 `019f94f3-c26a-7391-bbee-2d2fd4f8abb4`。README 已明确写“不要用”这条 SQL，实现却仍原样保留。生产错误空间现有 3,639 行来自另一条 Sina 路径，故本报告不把它们错误归因于此 loader。
- 建议:强制显式 `--workspace-id` 并验证其为运营空间；拒绝任何按 UUID 排序的隐式选择，整次装载应绑定可恢复批次或保持事务原子性。

### [HIGH]-14 智能资金引擎把 reboard_inferred 计入汇总，已经改变八席位净仓
- 位置:engine/smart_money.py:125-145；engine/smart_money.py:198-208；engine/smart_money.py:315-347；deploy/collector/compute-seat-totals.sql:44-52
- 失败场景:某会员某日只有部分合约存在反推行 → `member_day()` 把反推数量与官方逐合约数量一起求和 → 八席位净仓、净仓分位、成本和撤离判定使用“有时含反推、有时不含”的序列 → 产生虚假跳变；这与仓库 SQL 明定的“不进汇总”口径相反。
- 证据:`clean_seat()` 不过滤 `source='reboard_inferred'`，`member_day()` 对全部非汇总 long/short 行 `pivot_table(..., aggfunc='sum')`；compute-seat-totals 注释及谓词明确排除该源。生产只读 SELECT：AG 八席位 2023-04-18 含反推净仓 29,787，排除后 41,241，差 -11,454 手；AU/AG 共 72,367 条反推 long/short，其中八席位 5,923 条。
- 建议:在智能资金的统一输入入口排除 `reboard_inferred`；品种净仓、long floor、成本和信号测试都加入官方行与反推行共存的夹具，并共用同一来源口径定义。

### [HIGH]-15 建仓过程只给席位去重，行情双源使同一交易日重复进入成本引擎
- 位置:rust/crates/database/src/spread_analytics.rs:2039-2080；rust/apps/api/src/spread_analytics.rs:778-815；rust/crates/domain/src/seat_cost.rs:77-181；frontend/src/views/SeatsView.vue:239-274
- 失败场景:`price_history` 同一合约同日同时有日更源与官方历史源 → `prices` CTE 不去重即连接席位 → 同一持仓日返回两行并依次进入成本引擎 → 前端出现重复日期、空 K 线和额外盈亏柱；若两源结算不同，同一自然日还会被当成两次价格变化，成本结果取决于无确定顺序的来源。
- 证据:席位 CTE 有 `distinct on`，行情 CTE 只是直接读 `price_history`，最终仅 `order by 1`。生产 AU/AG 有 140 个重复 `(contract,trade_date)`、涉及 7 日并影响 1,916 个会员序列日。实例 `AG2608/国泰君安/2026-08-05`：两行均 long=2074、short=192；akshare 行 OHLC 部分为空，official 行完整，settlement 都为 14668。第一行产生 12,876,480 当日盈亏，第二行又产生一根 0 盈亏柱。
- 建议:行情先按 `(contract,trade_date)` 用与自由价差相同的来源优先级收敛为一行，再连接席位；增加真实 PostgreSQL 双源行为测试，断言一个日期只产出一个成本点。

### [MEDIUM]-16 collector 凭据在备份和数据库 provision 前落盘，失败不会撤回
- 位置:.github/workflows/deploy-futures.yml:673-701；.github/workflows/deploy-futures.yml:777-780；.github/workflows/deploy-futures.yml:543-551
- 失败场景:首次 provision 先生成 credential file → 随后 pg_dump 因磁盘满失败，或数据库账号创建后 E2E 失败并恢复到 provision 前备份 → 凭据文件永久保留而数据库账号不存在 → 下次默认 `provision_collector=false` 仅检查文件非空，日更登录失败。
- 证据:credential 在第 684 行原子 `mv` 落盘，备份从 691 行才开始，`SWITCH_STARTED` 到 701 行才置位；数据库账号在 777-780 才创建。任何回滚路径都不处理 credential 文件。
- 建议:先保留 candidate 临时凭据，账号成功后再提交；记录原文件是否存在，任何失败都恢复旧文件或删除新文件，并在结束前验证账号与凭据成对可用。

### [MEDIUM]-17 futures_migrator 在成功部署后永久保留 LOGIN 和全部对象所有权
- 位置:.github/workflows/deploy-futures.yml:704-749；.github/workflows/deploy-futures.yml:879-937；.github/workflows/deploy-futures.yml:487-490
- 失败场景:部署成功后，能连接 PostgreSQL 本地 trust socket 的进程指定 `-U futures_migrator` → 以数据库/schema/table owner 身份执行 ALTER/DROP/GRANT → 临时迁移权限越过部署窗口长期存在，runtime grant 边界无法限制该身份。
- 证据:成功路径执行 `alter role ... login` 并转移全部所有权，却没有对应 NOLOGIN；只有 rollback 执行 NOLOGIN。生产只读 catalog 输出 `migrator|rolcanlogin=true|rolsuper=false|rolbypassrls=false`，database、app/public schema 以及 57 张表的 owner 均是 futures_migrator；生产 `pg_hba.conf` 的本地连接为 trust。
- 建议:使用永久 NOLOGIN 的 owner role，部署登录角色只在迁移窗口临时获得成员资格；成功和失败出口都撤销登录能力并断言 `rolcanlogin=false`。

### [MEDIUM]-18 cron 验收漏掉 15:00 spread-warm 整行
- 位置:deploy/collector/futures-collector.cron:15-20；rust/tests/phase_4a_e2e.sh:35-41；ops/preflight-deploy.sh:136-147
- 失败场景:误删或修改 15:00 warm cron/command → preflight 只提取 09:30、13:30 两条 collector grep，E2E 也不检查 warm 行和 wrapper → 部署全绿，但价差从此不再自动预热。
- 证据:源 cron 有三条任务，E2E 只有两条 `grep -c`；preflight 的提取逻辑只会得到这两条。已修的“两条 collector cron 时刻漂移”没有覆盖第三条 warm 任务。
- 建议:比较规范化后的完整 cron 文件或使用声明式 schedule manifest 逐项验证，同时校验安装后的 wrapper hash。

### [MEDIUM]-19 Phase 5 阻断门硬编码外部品种 JM 与 09/01 月份
- 位置:rust/tests/phase_5a_e2e.sh:190-202；rust/tests/phase_5a_e2e.sh:273-286；.github/workflows/deploy-futures.yml:856-863
- 失败场景:三禾正常响应，但当天品种列表暂不含 JM，或 JM 可用月份不同时含 09/01 → API、数据库和 schema 都健康，验收仍失败 → 候选触发整库回滚。
- 证据:E2E 先读取动态 items，随后强制寻找 symbol JM、断言 months 同时包含 `09` 和 `01`，再固定查询 JM-09-01；preflight 无法预检外部响应，deploy 把任一失败作为 rollback 条件。
- 建议:生产阻断门从实时响应动态选择一个至少有两个月份的可查询组合；固定 JM-09-01 留在 mock/fixture 契约测试，不作为生产切换条件。

### [MEDIUM]-20 load_history.py 接受 --what dce 却成功执行空操作
- 位置:backfill/load_history.py:26；backfill/load_history.py:117；backfill/load_history.py:127-184
- 失败场景:运营者运行合法参数 `--what dce` → argparse 接受并成功连接数据库 → CZCE/SHFE/Sanhe 三个分支全部跳过 → 打印价格 0 行、席位 0 行并返回 0 → 人工或调度误判 DCE 回填成功。
- 证据:choices 明列 `dce`，`DCE_DIR` 只定义未使用；执行分支仅有 `czce/shfe` 与 `sanhe/all`，函数末尾固定返回 0。
- 建议:实现 DCE 分支；在此之前移除该 choice，并对零文件/零行处理返回明确失败。

### [MEDIUM]-21 智能资金会员别名表落后于 Rust 的统一别名表
- 位置:engine/smart_money.py:35-40；engine/smart_money.py:135；engine/smart_money.py:214-240；rust/crates/database/src/spread_analytics.rs:1830-1842
- 失败场景:AU/AG 新数据把国投写成已知变体 `国投安信期货` → Python 不归一该名称 → `detect_events()` 只遍历 group8 中的 `国投期货` → 该机构整段历史窗口、权重和事件分数被静默排除。
- 证据:Rust `MEMBER_ALIASES` 包含 `国投安信期货 -> 国投期货` 和 `申银万国 -> 申万期货`，Python alias 缺两项；本地最小输入经 `clean_seat` 后仍为 `国投安信期货` 且不在 group8。生产全表已有 34,590 行 `国投安信期货`（当前 AU/AG 尚无该变体），说明这是实际来源写法而非臆造字符串。
- 建议:会员别名只保留一个版本化来源，由 Rust 和 Python 生成或读取；用别名全集参数化测试归一结果与 group8 纳入行为。

### [MEDIUM]-22 点值事实维护三份，测试却只检查已被后续迁移纠正的旧迁移
- 位置:rust/migrations/202608100003_instrument_price_multiplier.sql:33-44；rust/migrations/202608100006_price_multiplier_seed_under_rls.sql:45-59；collector/tests/test_price_multiplier.py:21-41；collector/tests/test_price_multiplier.py:67-81
- 失败场景:最终生效迁移中的 JD 点值被误改为 5，而旧迁移和测试字典仍为 10 → CI 继续通过，因为测试只读取 202608100003 → 新库完整迁移后的最终点值为 5 → 鸡蛋一元、十手的盈亏从正确 100 元被算成 50 元。
- 证据:两份迁移分别复制八个点值，测试还有第三份 `EXPECTED`；定向测试只打开 202608100003，全仓没有测试查询应用完 202608100006 后的最终表。202608100006 的注释明确说明旧迁移因 RLS 曾实际播种零行，因此只测旧迁移文本并不验证最终状态。
- 建议:点值放到一个版本化规范源；测试在隔离 PostgreSQL 实际重放全部迁移后查询最终表，而不是匹配某个历史迁移文本。

### [MEDIUM]-23 Phase 4A 验收把 127 个历史手工批次当作系统不变量
- 位置:rust/tests/phase_4a_e2e.sh:161-172；rust/tests/phase_4a_e2e.sh:666-684；ops/preflight-deploy.sh:136-147
- 失败场景:生产按保留策略清理旧 E2E/手工批次，或从合法备份恢复到少于 127 个 manual batch → 业务与 schema 均正确，E2E 第 171 行仍失败 → 部署在最后阶段整轮回滚。
- 证据:验收硬编码 `test "$legacy_batches_before" -ge 127`；127 来自历史生产残留，后续 fingerprint 已足以验证本轮没有破坏既存批次。preflight 只比对 cron，不会发现该基线与生产数据生命周期漂移。
- 建议:删除绝对历史数量门槛，改为“本轮前后 count/fingerprint 不变”或由部署前动态记录的受保护集合驱动。

### [MEDIUM]-24 运营用 load_sanhe_seats.sql 既不进发布包，也不受清单门禁
- 位置:backfill/load_sanhe_seats.sql:1-61；.github/workflows/deploy-futures.yml:206-217；ops/preflight-deploy.sh:123-134；backfill/README.md:122-126
- 失败场景:需要在生产修正或重灌三禾席位历史，或迁移到新机器 → 仓库有经审查 SQL，标准 release 与服务器都没有该文件 → 运维只能临时手抄/手工上传或无法执行，发布 SHA 与实际运行 SQL 再次分叉。
- 证据:workflow 的 backfill 显式清单只安装 `.py`，preflight 也只枚举 `backfill/*.py`；生产只读 `ls` 在 `/opt/futures-platform/load_sanhe_seats.sql` 和 release 的 `backfill/` 下均未找到文件。README 却声明新增脚本必须进入显式清单，handoff 也把该 SQL 作为实际装载入口。
- 建议:把所有被文档或 wrapper 调用的 backfill SQL/数据清单纳入版本化 bundle 和安装表，并让 preflight 从同一 manifest 核完整性；无运行用途的文件则明确归档而不是保持半运营状态。

### [LOW]-25 preflight 读取构建日志失败时静默退出
- 位置:ops/preflight-deploy.sh:14；ops/preflight-deploy.sh:159-168
- 失败场景:GitHub CLI/API 短暂故障或日志权限变化 → 脚本先打印“镜像构建成功”，随后 `gh run view --log` 非零 → stderr 被丢弃且 `set -e` 直接终止 → 没有 `fail()`、没有失败汇总，操作者只能看到流程突然消失。
- 证据:第 166 行命令替换没有 `||` 或显式状态处理，并把 stderr 重定向到 `/dev/null`；相邻 CI/build-id API 调用都有 fallback。该问题不改变生产状态，但显著延长部署故障定位。
- 建议:显式捕获日志读取状态，调用 `fail` 并保留脱敏后的错误原因；只有拿到完整、可解析日志才继续提取 digest。

## A-F 覆盖声明

### A. 部署与发布链

已逐行检查 `.github/workflows/deploy-futures.yml`（含内嵌 remote script）、`container-images.yml`、`ci.yml`、`ops/preflight-deploy.sh`、两套 E2E、三份 collector wrapper、cron、compose、Dockerfile 与发布清单；生产只读核对了 cron、wrapper 锁、stable/evidence 时间、容器环境、运行镜像与角色状态。确认发布包覆盖 `deploy/collector/*` 和显式 backfill Python，但遗漏 `backfill/load_sanhe_seats.sql`；仓库 engine 生产入口不在任何发布单元。两个已知手工 cron 不另立 finding，仍列为换机/回滚的仓库外资产。未触发 workflow/部署，未模拟删库、磁盘满或断网，也未穷举 VPS 上与本仓无引用关系的全部手工文件。

### B. 只在生产才炸的路径

已提取 Rust API/Worker 的 INSERT、UPDATE、DELETE 表，与 migrations 和生产 `futures_runtime` 权限逐项核对；生产缺权限查询无输出，列级 UPDATE 授权覆盖实际更新列，未发现第四个 runtime grant 缺口。生产角色为 `super=false,bypassrls=false`；正式 workspace 业务表均 ENABLE/FORCE RLS 且有 policy。以 `futures_runtime + set_config` 在只读事务抽查 price/seat/scope/instruments/monitor，未发现新的非 leakproof WHERE 或 SQLx 类型错误；numeric 读取均以 text 或显式 numeric 边界承载。未在生产执行任何写路径，未读取 secret 内容，因此不能用本次审查证明写权限路径的动态成功。

### C. 幂等与重复执行

静态逐个检查全部 `deploy/collector/*.sql`：monitor 与 infer 脚本会先清受管窗口，seat totals 会先删后建；两份 Sanhe 修复脚本在修复后不再命中。`project-history.sql` 与 `load-dce-daily.sql` 的范围缩小/回滚陈行问题已分别报告。delete/reinsert 脚本会刷新 UUID/时间戳，严格说不满足逐字节一致，但未发现消费者依赖内部 ID，故没有另立 finding。全部 bundle migration 都有事务和 schema_versions 门禁，生产已有仓库列出的版本且无缺口。受“生产只读、只写报告”边界约束，未真实连跑 SQL 两次，也未在生产或本地新建数据库做全量 migration 重放。

### D. 同一事实两处维护

核对了会员别名、行情/席位来源优先级、八品种点值、产品 scope、监控范围、主力月份，以及 build/reuse/deploy 路径清单。Rust 内 `MEMBER_ALIASES` 同时生成 SQL/Rust 归一是正例；Python engine 别名、点值迁移/测试、镜像输入 allowlist 和 E2E 来源集合是反例，均已有具体 finding。历史迁移为审计记录而合理保留的旧值没有一概当作缺陷；只在测试继续锚定失效旧版本时报告。

### E. 测试盲区

逐项检查了 vue-tsc/Vitest、Vite build、Cargo、collector pytest、shellcheck、compose config 与独立 image workflow。Vite build 已覆盖用户列出的模板闭合事故，未重报。现有 Cargo 测试多处只匹配 SQL/源码字符串：席位 distinct-on 和 NULL 文本测试都无法发现行情双源重复；shellcheck 不解析 YAML 内嵌 remote script，也不覆盖 engine wrapper；engine Python 没有 CI 编译/lint/行为测试。标准 preflight 又跳过 live collector，而打开 full gate 会被陈旧来源断言挡死。为避免除报告外的文件写入，本次没有实际运行会生成构建/缓存产物的测试套件，也未触发任何 workflow。

### F. 数据正确性抽查

检索并走查所有 `seat_history` 读写引用，覆盖 Rust 席位列表/日期/建仓、SQL 汇总与反推、repo `engine/`、backfill/官方装载及前端三态传播。Rust 主路径能区分整日掉榜 NULL 与真实 0，单侧缺榜按 0 是需求文档明确口径，未误报；`reboard_inferred` 在 SQL totals 正确排除，但 engine 不一致；来源优先级在自由价差路径正确收敛，建仓行情路径漏收敛；混合 NULL 的 change 汇总已在生产复现。未审查生产 smart-money 工作区内部逻辑，也未重放完整生产 API 请求。

## Top 3 系统性根因

1. **发布原子边界不完整。** 数据库、容器、固定路径 host 文件、stable 指针和凭据分别提交，回滚只覆盖其中一部分，导致“文件新、库旧”“指针新、服务旧”等可持续中间态。
2. **关键事实依赖多份手工 allowlist/常量同步。** 镜像输入、发布清单、来源 code、会员别名、点值与 cron 断言散落在 YAML、Shell、SQL、Rust、Python 和文档中；没有机器可消费的单一 manifest。
3. **验收路径不等于生产路径。** 服务以 acceptance 环境长期运行，标准入口跳过 live collector，部分验收依赖超级用户、历史残留计数或实时第三方固定品种，测试又偏向源码字符串而非 runtime 角色下的真实数据库行为。

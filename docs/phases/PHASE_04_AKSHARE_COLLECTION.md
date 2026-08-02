# Phase 4 akshare 采集与回填

状态：Phase 4A Planner 契约已确认；本单授权在契约提交推送后直接实施 Phase 4A。
基线：`main@2a6e6d5`。
依据：`DEC-031`、`DEC-038`、`DEC-039`、`DEVELOPMENT_PLAN.md` 第 5 节、`DATABASE_DESIGN.md` 第 5、6、9、11 节。

## 1. 阶段边界

### 1.1 Phase 4A（本单）

- 建立独立、一次性运行的 Python `collector` 容器；Python 与 akshare 依赖全部锁定版本。
- 支持 `--date YYYY-MM-DD` 指定单个日期；默认日期由调用方显式传入最近交易日，不在采集器内悄悄回退日期。
- 一次调度覆盖 DCE、SHFE、CZCE、GFEX、CFFEX，按交易所相互隔离采集目录、日行情、席位龙虎榜和当日交易日历。
- 标准化结果写 CSV，再由专用 `analyst` 服务账号登录平台并执行上传、自动确认；采集器不持有数据库凭据，也不直接连接数据库。
- Rust 导入管线新增固定数据集处理器，将成功自动批次写入正式业务表；保留来源、批次、行变更与整批回滚链路。
- VPS host cron 在每个交易日盘后 17:30、21:30 各运行一次 `docker compose run --rm collector`；两次执行及人工重跑均幂等。
- 将 `collector` 镜像加入 CI 构建和 `container-images` 发布；部署后完成真实最近交易日 E2E、故障隔离、RLS、来源追溯与内存峰值取证。

### 1.2 Phase 4B（不在本单）

- 不执行任何批量历史回填。
- 不实现行情全历史任务编排、席位近五年优先/逐年向前补拉、断点游标和不可得年份登记任务。
- 不执行跨年份完整交易日历回填。
- Phase 4A 的单日期 CLI、固定模板、幂等键和正式表处理器必须可被 4B 复用，但不得以隐藏循环提前执行 4B。

### 1.3 其他非目标

- 不实现套利、分析、图表、AI 或新前端页面。
- 不改变现有手动文件导入的 inspect、mapping、preview、validate、人工 confirm 流程。
- 不恢复已废止的浏览器、远程桌面、OCR 或服务端图表渲染基础设施。
- 不清理 VPS 既有数据；Phase 3 遗留的 127 个测试批次保持原状。

## 2. 运行架构与信任边界

```text
VPS host cron
  -> docker compose run --rm collector --date YYYY-MM-DD
      -> AKShare -> 五交易所白名单公开接口/文件
      -> /work/*.csv（容器临时文件，退出即删除）
      -> 平台 login -> upload(automatic) -> automatic-confirm
          -> 固定模板解析/校验
          -> job_queue(import_commit)
          -> worker 数据集处理器
          -> 正式业务表 + import_row_changes + 来源/批次链路
```

- `collector` 非常驻、无 restart policy、不加入数据库内部网络，只加入可到达平台 API 的 edge 网络；初始内存限制 `512m`。
- 凭据文件固定为宿主机 `/etc/futures-platform/secrets/collector-credentials`，权限 `0400 root:root`，只读挂载到 collector 的 `/run/secrets/collector-credentials`。容器初始仅保留读取后降权所需的 `SETUID`/`SETGID` capability，读取后立即切换至 uid/gid 10001；其余 capability 全部移除且保持 `no-new-privileges`。文件只包含平台 API 地址、服务账号标识和密码；不进入 Git、镜像、标准化 CSV、批次元数据或日志。
- collector 日志只能记录日期、交易所、数据集、行数、批次 ID、状态和稳定错误码；禁止记录请求 Cookie、CSRF 值、密码、完整认证响应或凭据文件内容。
- 服务账号由既有管理员流程创建，角色固定为 `analyst`。管理命令只绑定唯一启用的 admin 所有者 Workspace；历史验收产生的 analyst/viewer Workspace 不参与选择，候选为零或多个时必须失败，不得隐式选择首行。本阶段不新增隐藏管理员、bootstrap token 或数据库账号路径。
- collector 先登录取得 session cookie，再取得 CSRF，随后上传和自动确认。自动确认仍执行 `Origin`、CSRF、Workspace、RBAC、来源白名单和固定模板校验。
- 自动批次的来源错误按“交易所 × 数据集 × 日期”隔离。一个交易所断网或解析失败会登记对应批次为 `failed` 且正式表零写入，其他交易所继续运行。

## 3. 数据源白名单与实际 AKShare 函数

仅允许下表函数产生 4A 数据。允许域名登记到 `data_sources`，collector 启动时同时以代码常量校验；重定向后的最终主机仍必须在同一交易所白名单内。

| 交易所 | 目录函数 | 日行情函数 | 席位函数 | 允许直连域名 |
| --- | --- | --- | --- | --- |
| DCE | `futures_contract_info_dce()` | `get_dce_daily(date)` | `futures_dce_position_rank(date)` | `www.dce.com.cn`, `portal.dce.com.cn` |
| SHFE | `futures_contract_info_shfe(date)` | `get_shfe_daily(date)` | `get_shfe_rank_table(date)` | `www.shfe.com.cn`, `tsite.shfe.com.cn` |
| CZCE | `futures_contract_info_czce(date)` | `get_czce_daily(date)` | `get_rank_table_czce(date)` | `www.czce.com.cn` |
| GFEX | `futures_contract_info_gfex()` | `get_gfex_daily(date)` | `futures_gfex_position_rank(date)` | `www.gfex.com.cn` |
| CFFEX | `futures_contract_info_cffex(date)` | `get_cffex_daily(date)` | `get_cffex_rank_table(date)` | `www.cffex.com.cn` |

- 不调用聚合入口 `get_futures_daily` 或 `get_rank_sum`，以便单交易所失败可独立归因和隔离。
- 不调用新浪、东方财富、生意社等二手源。当前实现不存在待用户裁定的聚合源。
- akshare 自带交易日判断只用于拒绝明显非交易日参数；正式 `trading_calendar_days` 的 4A 记录由该交易所目录/行情官方响应共同证明，不能仅凭 akshare 本地静态日历入库。
- 五个来源代码固定为 `akshare_dce_official`、`akshare_shfe_official`、`akshare_czce_official`、`akshare_gfex_official`、`akshare_cffex_official`；`source_type=exchange_public`、`authorization_status=whitelisted`、`connector_code=akshare_v1`。

## 4. 标准化 CSV 契约

所有 CSV 为 UTF-8、RFC 4180 兼容、有表头。空值为空字段，不用 `0`、`-` 或 `null` 冒充。金额和数量不使用千位分隔。每行都含 `source_record_ref`，其值是公开响应内可复现的合约/日期/排名定位符，不含 URL 查询秘密。

### 4.1 `futures_catalog_v1`

| CSV 字段 | 类型/规则 | 正式字段 |
| --- | --- | --- |
| `exchange_code` | DCE/SHFE/CZCE/GFEX/CFFEX | `exchanges.code` |
| `exchange_name` | 交易所正式中文名 | `exchanges.name` |
| `timezone` | 固定 `Asia/Shanghai` | `exchanges.timezone` |
| `instrument_code` | 从官方产品代码或合约代码确定，统一大写 | `instruments.code` |
| `instrument_name` | 官方品种名；缺失时等于代码并产生质量警告 | `instruments.name` |
| `currency_code` | 官方币种；未提供的境内期货固定 `CNY` | `instruments.currency_code` |
| `contract_multiplier` | 正数 decimal；源未提供可空并警告 | `instruments.contract_multiplier` |
| `price_tick` | 正数 decimal；源未提供可空并警告 | `instruments.price_tick` |
| `contract_code` | 官方合约代码，统一大写 | `contracts.code` |
| `delivery_month` | 从合约代码/官方月份标准化为 `YYYY-MM`；无法可靠确定时为空并警告 | `contracts.delivery_month` |
| `listed_at` | ISO date，可空 | `contracts.listed_at` |
| `expires_at` | 官方最后交易日/到期日，ISO date，可空 | `contracts.expires_at` |
| `source_record_ref` | `exchange:contract:as_of_date` | 批次行追溯元数据 |

目录处理器必须严格按 exchange → instrument → contract 顺序 upsert。键分别为 `(workspace_id, code)`、`(workspace_id, exchange_id, code)`、`(workspace_id, instrument_id, code)`；重复目录只补齐或刷新同一行，不创建副本。

### 4.2 `trading_calendar_v1`

| CSV 字段 | 类型/规则 | 正式字段 |
| --- | --- | --- |
| `exchange_code` | 五交易所代码 | 解析 `exchange_id` |
| `calendar_version` | `akshare-v1:<exchange>:<YYYY-MM-DD>` | `trading_calendar_versions.version` |
| `effective_from` | 指定日期 | `trading_calendar_versions.effective_from` |
| `trade_date` | 指定日期 | `trading_calendar_days.trade_date` |
| `is_trading_day` | 仅官方响应证明有当日数据时为 `true` | `trading_calendar_days.is_trading_day` |
| `day_session_json` | 固定 `{}`，详细时段留后续版本 | `trading_calendar_days.day_session_json` |
| `night_session_json` | 固定 `{}`，详细时段留后续版本 | `trading_calendar_days.night_session_json` |
| `source_record_ref` | `exchange:calendar:date` | 批次行追溯元数据 |

版本键为 `(workspace_id, exchange_id, version)`，日期键遵循 `DATABASE_DESIGN.md`：`(workspace_id, calendar_version_id, trade_date)`。4A 不因交易所无数据而臆造休市日；无官方证明时对应日历批次失败隔离。完整历史日历由 4B 回填。

### 4.3 `daily_market_prices_v1`

| CSV 字段 | 类型/规则 | 正式字段 |
| --- | --- | --- |
| `exchange_code` | 五交易所代码 | 目录解析 |
| `contract_code` | 已标准化大写 | `contract_id` |
| `trade_date` | ISO date，必须等于任务日期 | `market_prices.trade_date` |
| `session_type` | 固定 `daily` | `market_prices.session_type` |
| `observed_at` | 数据获取 UTC 时间 | `market_prices.observed_at` |
| `granularity` | 固定 `1d` | `market_prices.granularity` |
| `close_price` | decimal，可空但不可与 settlement 同时空 | `market_prices.close_price` |
| `settlement_price` | decimal，可空但不可与 close 同时空 | `market_prices.settlement_price` |
| `currency_code` | 固定/官方 `CNY` | `market_prices.currency_code` |
| `calendar_version` | 对应 4.2 版本 | `calendar_version_id` |
| `revision_no` | 固定 `1`；Phase 4A 重跑不递增 | `market_prices.revision_no` |
| `source_record_ref` | `exchange:contract:date:daily` | 批次行追溯元数据 |

业务唯一键固定为 `(workspace_id, source_id, contract_id, trade_date, session_type, granularity, revision_no)`。同日重跑使用 `ON CONFLICT DO NOTHING`；若同键值与已有值不同，记录 `source_revision_conflict` 错误，不静默覆盖。

行情入库前必须已存在对应 exchange/instrument/contract 和 calendar version。CSV 中不存在的合约形成 `unknown_contract` 行级错误；该行不写入，但不阻塞同批其他有效行。若批次存在其他结构或必填校验错误，则按 DEC-038 整批 `failed`、正式表零写入。

### 4.4 `seat_positions_v1`

AKShare 每个原始排名行可能同时给出不同会员的成交量、持买和持卖排名，标准化时拆为最多三行。

| CSV 字段 | 类型/规则 | 正式字段 |
| --- | --- | --- |
| `exchange_code` | 五交易所代码 | 目录解析 |
| `contract_code` | 已标准化大写 | `contract_id` |
| `trade_date` | ISO date | `seat_positions.trade_date` |
| `seat_name` | 官方原始会员简称，trim 后非空 | `seat_entities.canonical_name` |
| `rank_type` | `volume` / `long` / `short` | `seat_positions.rank_type` |
| `rank` | 正整数 | `seat_positions.rank` |
| `volume` | `rank_type=volume` 时非负整数，否则空 | `seat_positions.volume` |
| `long_position` | `rank_type=long` 时非负整数，否则空 | `seat_positions.long_position` |
| `short_position` | `rank_type=short` 时非负整数，否则空 | `seat_positions.short_position` |
| `source_record_ref` | `exchange:contract:date:rank_type:rank` | 批次行追溯元数据 |

`seat_entities` 以 `(workspace_id, canonical_name)` 幂等自动创建，`canonical_name` 等于原始席位名、`status=unreviewed`；别名合并与身份治理留 Phase 7。席位业务键固定为 `(workspace_id, source_id, trade_date, contract_id, seat_id, rank_type, rank)`。

## 5. 自动导入 API 与事务语义

### 5.1 外部调用

1. `POST /api/v1/auth/login`，建立 analyst session；随后取得 CSRF。
2. `POST /api/v1/imports` 上传 CSV，并带 `ingestion_mode=automatic`、固定 `dataset_type`、`data_source_code`、`collection_date`、`template_version`。上传响应返回 `import_id`。
3. `POST /api/v1/imports/{import_id}/automatic-confirm`，带 Origin、CSRF 和 Idempotency-Key。该入口只接受 `ingestion_mode=automatic`、白名单来源和服务端内建模板版本，不接受客户端 mapping JSON。
4. collector 轮询现有 import 状态；终态为 `succeeded`、`failed` 或 `dead_letter`。退出码只汇总，不因单一来源失败提前终止其他交易所。

`automatic-confirm` 在服务端完成 inspect、固定映射、全量校验和 enqueue，不创建人工 preview，也不要求人工 confirm。它不是绕过验证的捷径；手动批次访问该入口返回稳定错误 `automatic_mode_required`。

### 5.2 固定模板

- 模板版本代码固定为 `futures_catalog_v1@1`、`trading_calendar_v1@1`、`daily_market_prices_v1@1`、`seat_positions_v1@1`。
- 模板由迁移/服务端常量注册且不可由 collector 修改；Workspace 内首次使用按固定定义绑定。
- dataset handler 只按 batch 保存的模板版本和 dataset type 分派。未知版本必须使批次失败，不能退回 generic handler。

### 5.3 原子性、回滚和错误

- 每个 import commit job 在一个数据库事务中设置 `SET LOCAL app.workspace_id`，显式限定 Workspace，并在同一事务写正式表与 `import_row_changes`。
- 除 `unknown_contract` 是明确允许跳过的行级错误外，解析/校验失败使整个批次 `failed` 且该批正式表零变更。
- 质量警告（目录名回退、可选合约参数缺失、价格质量异常）写 `import_errors`/审计元数据，severity 为 warning，不阻断。
- `import_row_changes` 对 insert 写 after snapshot，对允许更新的目录行写 before/after snapshot 和 row-version fence；现有整批回滚预检、冲突整体中止和执行器恢复语义继续适用。
- 重放同一 automatic-confirm Idempotency-Key 返回同一 job；同批异参复用冲突；并发确认最多一个 job/一个对象决议。
- 网络/解析错误发生在 CSV 上传前时，collector 仍调用受控失败登记入口创建 `failed` import batch，保存稳定错误码、日期、交易所和数据集，不保存异常响应正文或凭据。

## 6. 数据库迁移要求

- 新建本契约所列 `data_sources`、目录、日历、行情、席位和必要的 `extraction_jobs` 表；已有表则仅做兼容扩展，不重复定义。
- 所有 Workspace 业务表自创建起 `ENABLE ROW LEVEL SECURITY` 且 `FORCE ROW LEVEL SECURITY`；策略使用 `app.current_workspace_id()`，所有外键同时验证 Workspace 归属。
- 为第 4 节全部业务键建立唯一约束；`market_prices` 和 `seat_positions` 必须引用 `source_import_batch_id` 或等价不可变批次关联，以便逐行来源追溯。
- 自动批次标记、来源、collection date、固定 template version 必须落在批次/任务元数据中；不得把账号、Cookie、CSRF 或密码写入数据库。
- migration、runtime role grant、RLS 跨 Workspace 读写拒绝和唯一键测试必须与代码同提交。

## 7. collector 工程契约

- 目录：`collector/`；入口 `python -m futures_collector`。
- 锁文件必须精确锁定 Python 基础镜像 digest 或 patch 版本以及 akshare、pandas、requests、httpx 等全部传递依赖；CI 禁止浮动安装。
- CLI 最少支持 `--date YYYY-MM-DD`、`--exchange all|DCE|SHFE|CZCE|GFEX|CFFEX`、`--dataset all|catalog|calendar|market|seats`。生产 cron 固定使用 `all`。
- 采集层、标准化层、CSV 层、API client 分离；AKShare 网络调用通过 adapter 注入，单元测试全部 mock，不依赖公网。
- 域名白名单在请求适配器和重定向检查中强制执行；拒绝 loopback、RFC1918、link-local、metadata address 和非白名单最终主机。
- 临时目录固定 `/work`，容器 `read_only`，只给 `/tmp` 和 `/work` tmpfs；非 root 用户运行；退出后不保留 CSV。

## 8. Compose、镜像、cron 与部署

- `docker-compose.yml` 增加 `collector` 一次性 service，不设 `restart`、`depends_on: postgres` 或数据库网络；`mem_limit: 512m`，凭据单文件只读挂载。
- `docker-compose.production.yml` 将 collector 指向 `...-collector:sha-<git_sha>`/不可变 digest。发布工作流增加 collector 镜像矩阵项，部署工作流增加 collector digest 输入、校验、拉取、release compose 固定和取证。
- 部署默认不改写凭据内容；部署前只校验宿主凭据文件存在、owner/mode 正确。若不存在，停止并由管理员执行管理流程。首次上线允许部署者显式传入 `provision_collector=true`，在服务切换前调用同一受控管理命令一次性创建 root:0400 文件与固定 analyst 账号；默认值必须为 `false`，已存在账号只校验、不重置口令。
- host cron 配置文件由发布包提供并在 VPS 安装，至少包含北京时间 17:30、21:30 两条；使用 flock 防止重叠，日志进入受控 journald/轮转文件且不含凭据。
- 记录 `docker stats --no-stream`/cgroup 峰值；若真实峰值接近 512m，只能基于证据调整并在交接列明。

## 9. 提交拆分

1. `docs: define phase 4a akshare collection contract`（本文件，先推送）。
2. `feat: add phase 4a market dataset schema`（迁移、RLS、唯一键、正式表 repository/测试）。
3. `feat: add automatic dataset import handlers`（API、固定模板、worker handlers、回滚/追溯测试）。
4. `feat: add akshare collector`（Python 包、锁依赖、解析/标准化/客户端测试）。
5. `ci: publish and deploy collector image`（Docker、Compose、CI/images/deploy、cron bundle）。
6. `test: add phase 4a collection e2e`（本地/部署 E2E 与失败注入）。
7. `docs: record phase 4a delivery evidence`（部署文档、交接、`LATEST.md`）。

如实际依赖关系要求，可拆得更细，不得把不相干业务代码混入。

## 10. 验收命令与证据

本地必须贴实际摘要：

```bash
cd rust && cargo +stable fmt --check
cd rust && cargo +stable clippy --workspace --all-targets -- -D warnings
cd rust && cargo +stable test --workspace
pnpm lint
pnpm test
pnpm build
python -m ruff check collector tests
python -m pytest
git diff --check 2a6e6d5..HEAD
docker compose config --quiet
docker compose -f docker-compose.yml -f docker-compose.production.yml config --quiet
```

CI 必须在当前 Phase 4A 分支成功；`container-images` 必须发布 api、worker、frontend、collector 四个不可变镜像并记录 digest。

VPS 部署后 E2E 必须：

- 证明运行版本和四镜像 digest 与发布候选一致，且未泄露凭据。
- 对最近交易日真实运行一次，五交易所行情与席位批次均 `succeeded`；`market_prices`、`seat_positions` 行数均大于 0；目录和当日日历存在。
- 重跑同日后两张正式表计数不变，业务唯一键重复数为 0。
- 以受控网络故障注入一个交易所，对应来源批次 `failed` 且正式表零写，其余交易所成功且不受影响。
- 抽查 RLS 跨 Workspace 拒绝、正式记录到 import batch/data source/CSV 行的追溯、自动批次无 preview、整批 rollback-check 可用。
- 证明 cron 两个时点已安装、flock 生效、collector 内存峰值已记录。
- 前后对比确认既有 users 和 127 个测试 import batches 未被清理或篡改；Phase 4A 新批次的增加必须逐项可解释。
- Phase 4A 生产部署不得重跑会向生产库保留新手动批次的 Phase 3C/3D 历史 E2E；其回归由本地/CI 与既有 Phase 3 收口证据覆盖。VPS 只运行本阶段 E2E，部署报告必须如实标记历史 E2E 未重跑。

## 11. 回滚方案

- 应用回滚：使用部署流程切回上一稳定的 api/worker/frontend release；collector 为一次性服务，先禁用 Phase 4A cron，再切回 Compose release。
- 数据回滚：对已成功 Phase 4A 批次使用现有整批 rollback-check/rollback；任一后续修改或依赖冲突时整体零变更，不做手工部分删除。
- migration 不做破坏性 down；新表保留以便审计，旧应用无法访问新入口但不受影响。
- 凭据撤销由管理员禁用 collector 服务账号并移除宿主机凭据文件；不得在回滚日志中复制文件内容。

## 12. 完成定义

只有第 10 节全部本地、CI、镜像、VPS、幂等、失败隔离、RLS、追溯、cron 与内存证据通过，并完成不可覆盖交接及 `docs/handoffs/LATEST.md` 更新，Phase 4A 才可交给独立 Evaluator。Phase 4B 仍保持未实施状态。

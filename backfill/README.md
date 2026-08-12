# 历史回填

把三家交易所与三禾的原始文件，灌进 `price_history` 与 `seat_history` 两张表。
设计见 `docs/TWO_TABLE_DESIGN.md`，各家原始字段见 `docs/RAW_FIELD_INVENTORY.md`。

顺序是运营者定的：**先采原始文件 → 再看清楚里面是什么 → 再建表 → 最后灌**。
反过来做的代价，2026-08-10 一天之内验证过四次：每次都是灌到一半才发现表里少一列。

## 灌库前必读：workspace 挑哪一个

**不要用 `select id from workspaces order by id limit 1`。**

生产上有 31 个 workspace，绝大多数是历次验收留下的 E2E 空间。按 UUID 排序取第一个
挑中的是 `Phase 3C E2E 1`，于是 2026-08-10 那次回填的 23.5 万行价格和 380 万行席位
全落在一个测试空间里，运营者的个人 Workspace 只有每日采集写进去的最近八天——
页面上看起来「几乎没有数据」，而库里明明躺着十三年。这个错不报任何异常。

正确判据：**有 `market_prices` 的那个 workspace**。每日采集是以运营者的账号登录写
进去的，所以有行情的空间必然是他在用的。

```sql
-- 装载时这样限定
where exists (select 1 from market_prices m where m.workspace_id = <目标>)
```

另一个相关的坑：`product_instrument_scope` 是**逐 workspace** 播种的，每个空间都有
一份。join 它而不限定 workspace，一份 CSV 会被复制成 31 份——同一天发生过，
3639 行灌出 112809 行。


## 用法

脚本跑在 VPS 上（`/opt/futures-platform/`），三步各自可断点续跑。

```
# 1. 采原始文件。已存在的跳过，404 当作非交易日不重试。
python3 fetch_exchange.py czce 2012-12-01 2026-08-07
python3 fetch_exchange.py shfe 2008-01-01 2026-08-07
python3 sanhe_survey.py                     # 先筛出持有大商所三品种的会员

# 2. 解析成两张表形状的 CSV。--limit 只处理前 N 个文件，用于验收。
python3 to_csv.py --what all

# 3. 灌库（见下方 SQL），可重复执行，靠业务键 upsert 而不是追加。
```

## 落盘位置

| | 路径 |
| --- | --- |
| 郑商所 / 上期所 原始 | `/opt/futures-platform/exchange-raw/{czce,shfe}/{market,seats}/{日期}.{txt,dat}` |
| 三禾 原始 | `/opt/futures-platform/sanhe-seats/raw/{日期}/{会员}.json` |
| 大商所 年度文件 | `/opt/futures-platform/dce-history/{品种}_{年}.{xlsx,xls}` |
| 解析产物 CSV | `/opt/futures-platform/load/{price,seat}_{来源}.csv` |

## 灌库

```sql
create temp table p_stage (like price_history);
alter table p_stage drop column id, drop column workspace_id, drop column loaded_at;
\copy p_stage from '/tmp/p.csv' with (format csv, header true)

insert into price_history (id, workspace_id, exchange, instrument, contract, trade_date,
  open_price, high_price, low_price, close_price, settlement_price, prev_settlement_price,
  volume, volume_basis, turnover, open_interest, open_interest_change, source)
select gen_random_uuid(), (select id from workspaces order by id limit 1), s.* from p_stage s
on conflict (workspace_id, contract, trade_date, source) do update set ... ;
```

席位同理，冲突键是 `(workspace_id, trade_date, exchange, instrument, contract,
is_variety_total, rank_type, member, source)`。

**临时表不要写 `on commit drop`**：psql 默认每条语句一个事务，写了的话下一条语句就找不到它了。

## 各家格式里已经踩过的坑

这些都不是文档写的，是拿最老的文件跑出来的。只测近几年的文件，一个都发现不了。

| 坑 | 表现 |
| --- | --- |
| 郑商所 2015-11 前是 **GBK + 逗号分隔 + 无表头**，之后是 UTF-8 + 竖线 + 有表头 | 按新格式解析老文件：一行都出不来 |
| 郑商所老文件段头用 **TAB** 分隔，且**品种只有中文名**（`品种：玻璃`，不是 `品种：苹果AP`） | 品种识别不出来，整段丢失 |
| 郑商所 `FutureDataHolding.htm` 是 **412**，必须取 `.txt` | 席位一天都采不到 |
| 郑商所必须走 **https**，http 是 301 | 不跟随重定向就是空 |
| 上期所 `o_day` 只是**日**（`"20"`），完整日期在 `report_date` | 2015 年前的文件全部静默返回空 |
| 上期所 **2011 年的席位文件没有任何日期字段**，只能取自文件名 | 同上 |
| 上期所 **2011 年行情没有成交额列**（后来才加的） | 留空，不编 |
| 上期所 `PRODUCTID` 是 `au_f` 带后缀，**没有合约代码列**，要与 `DELIVERYMONTH` 拼 | |
| 上期所 `auall` **不是品种汇总排名**，只有两行：`RANK=-1` 名为「期货公司」、`RANK=0` 名为「非期货公司」 | 当成会员会凭空造出两个不存在的席位 |
| 三家**成交额单位不同**：大商所元，郑商所与上期所万元 | 不换算则相差一万倍 |
| 三家**成交量口径不同**：大商所双边，郑商所 2020 起单边 | 不换算，只记 `volume_basis` |
| 郑商所合约是 **3 位月份**（`AP501`），世纪按品种上市年份推 | 不能用「大于今年就减 100」猜 |
| 大商所年度文件 **2019 年改过表头**，且 2024 年发布为 `.xls` | |

## 这些脚本住在哪里：仓库是唯一来源

采集与解析用的 Python 同时存在两处：

- 仓库 `backfill/*.py` —— **唯一来源，改动只改这里**
- 服务器 `/opt/futures-platform/*.py` —— 运行时的副本，由部署下发

**部署每次都会把仓库版覆盖到服务器上。** 这不是副作用，是这套安排的目的。

### 为什么要这样

2026-08-12 之前，服务器那份是一年前手工拷过去的，两边各自演化。后果是：服务器的
`parsers.py` 还在按「品种上市年」给三位郑商所代码补世纪，把 2026 年的 `FG608`
解析成 `FG1608`，跟真实存在过的 2016 年合约撞进同一条序列——行情错 96 行、席位
错 2958 行，**每天还在往里加**。仓库那份早就修成按交易日锚定，改动却从来没走到
机器上。同样的病 `run-backfill.sh` 也犯过一次，那处的注释还留着。

### 在服务器上改了脚本会怎样

**下次部署会把它还原成仓库版。** 但不会丢：

- 覆盖前若内容有差异，旧版先备份到 `/opt/futures-platform/.superseded/<sha>-<时间戳>/`
- 部署日志里会打一行 `BACKFILL_SCRIPT_SUPERSEDED <文件名>`

所以在机器上临时改脚本调试是可以的，**但调好之后必须回到仓库改一遍再发布**，
否则下一次部署就把它抹掉了。这条对另一个会话同样适用。

### 新增脚本时

加进 `.github/workflows/deploy-futures.yml` 的显式清单（搜 `backfill/parsers.py`
那一段）。忘了加会被 `ops/preflight-deploy.sh` 当场拦下——清单是显式的，就必须有
一道门盯着它完整。

### 换新机器时要装什么

`backfill/*.py` 会随发布自动到位，但它们依赖机器上另外两样东西，那两样**不在**
仓库里，得手工准备：

1. **原始文件归档** `/opt/futures-platform/exchange-raw/`（郑商所、上期所逐日文件）
   与 `/opt/futures-platform/sanhe-seats/raw/`（三禾）。这是几年的抓取积累，重抓
   一次要很久，**迁机器时直接整目录拷过去**。
2. **Python 依赖**：`psycopg2`、`akshare`、`pandas`。日更那条链路是跑在 collector
   镜像里的（镜像自带 pandas），只有手工跑 `load_history.py` 这类脚本才需要宿主机
   装依赖。

另外三份**不随发布下发**、只在服务器上手工装的东西，换机器要记得补：

| 文件 | 作用 |
| --- | --- |
| `/etc/cron.d/futures-official-seats` | 官方席位每日增量（09:55 / 13:55 UTC） |
| `/etc/cron.d/futures-smart-money` | 机构资金引擎（10:10 / 14:10 UTC） |
| `/usr/local/sbin/run-official-seats` | 上面那条 cron 调的脚本，源在 `engine/` |

**cron 里的时刻一律写 UTC。** `CRON_TZ` 在这台机器上不生效：写 `CRON_TZ=Asia/Shanghai`
配 `30 17`，实测它在 17:30 UTC 触发，也就是北京次日凌晨。这个坑在三个 cron 文件上
各踩过一次。

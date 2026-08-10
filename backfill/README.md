# 历史回填

把三家交易所与三禾的原始文件，灌进 `price_history` 与 `seat_history` 两张表。
设计见 `docs/TWO_TABLE_DESIGN.md`，各家原始字段见 `docs/RAW_FIELD_INVENTORY.md`。

顺序是运营者定的：**先采原始文件 → 再看清楚里面是什么 → 再建表 → 最后灌**。
反过来做的代价，2026-08-10 一天之内验证过四次：每次都是灌到一半才发现表里少一列。

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

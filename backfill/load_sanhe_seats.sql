-- 把三禾采到的大商所席位历史灌进 seat_history。
--
-- 数据来路：交易所自己不给焦煤/鸡蛋/生猪的席位历史（脚本 412、真实浏览器 500），
-- 东财只到 2025-11 且残缺。三禾是唯一覆盖到 2023-08 的来源，按会员逐日采集，
-- 见 `backfill/sanhe_seats.py` 与 `backfill/sanhe_to_csv.py`。
--
-- **rank 一律为空**：三禾按会员组织，给的是该会员的真实持仓，比交易所的「前 20」
-- 更全，代价是没有名次。空着是如实，不编。

-- 用法(先把 CSV 送进容器的这个**固定**路径)：
--   docker cp load/seat_sanhe.csv <容器>:/tmp/seat_sanhe.csv
--   psql … < backfill/load_sanhe_seats.sql
--
-- 路径为什么不做成参数:**`\copy` 是 psql 元命令，整行原样当参数，不做变量替换**
-- (手册原话:neither variable interpolation nor backquote expansion are performed)。
-- 2026-08-31 我改成 `\copy … from :'csv_path'` 并传了 -v，psql 把它当成一个名叫
-- `:` 的文件,报 `:: No such file or directory`。改用服务端 COPY 才能插值，但那要
-- pg_read_server_files 权限,不值得为一个路径去放权限。
-- 原来写死的是 '/tmp/seat_dce.csv' —— 那是「只有大商所三品种」时代的名字,现在
-- 同一套解析器也装铁矿石,沿用会让下一个人以为这脚本只管那三个品种。

\set ON_ERROR_STOP on

begin;

create temp table sanhe_stage (like seat_history);
alter table sanhe_stage drop column id, drop column workspace_id, drop column loaded_at,
  drop column updated_at;

\copy sanhe_stage from '/tmp/seat_sanhe.csv' with (format csv, header true, null '')

insert into seat_history (
    id, workspace_id, exchange, instrument, contract, is_variety_total,
    variety_total_is_computed, trade_date, rank_type, rank, member, quantity, change, source,
    updated_at
)
select gen_random_uuid(), w.id, s.*, now()
  from (
      -- 同一天同一榜同一会员可能因重复采集出现两次；冲突键里带 source，
      -- 重复行会在 on conflict 上互相打架，所以先去重再进正式表。
      select distinct on (trade_date, exchange, instrument, contract, is_variety_total,
                          rank_type, member, source) *
        from sanhe_stage
  ) s
  cross join (
      -- 灌进**真正在用的**那个 workspace，不是 UUID 最小的那个。
      --
      -- 生产上有 31 个 workspace，绝大多数是历次验收留下的 E2E 空间。回填装载脚本
      -- 原来用 `order by id limit 1`，挑中的是 Phase 3C E2E 1——十三年的数据因此
      -- 落在运营者看不见的地方，页面上是「几乎没有数据」，而库里躺着 380 万行。
      -- 这个错不报任何异常，是逐个 workspace 数行数才发现的。
      --
      -- 判据用「哪个空间真有行情」：每日采集以运营者的账号登录写入，
      -- 有行情的那个空间必然是他在用的。
      --
      -- **原来读 market_prices，那张表已经不存在了**(DEC-049,导入通道拆除时
      -- 随之删除)。这份脚本自那以后没再跑过，所以没人发现——照原样执行只会
      -- 得到 `relation "market_prices" does not exist`。同一个判据现在落在
      -- price_history 上，那正是直灌写入的目标表(run-official-seats 早已改过，
      -- 这份漏了)。**手工装载脚本不在任何流水线里，改表结构时最容易漏掉它们。**
      select p.workspace_id as id
        from price_history p
       group by 1
       order by count(*) desc
       limit 1
  ) w
on conflict (workspace_id, trade_date, exchange, instrument, contract,
             is_variety_total, rank_type, member, source) do update set
    rank = excluded.rank,
    quantity = excluded.quantity,
    change = excluded.change,
    -- loaded_at 保持首次入库不动、updated_at 刷新,口径见 load-seats-direct.sql。
    updated_at = now();

commit;

-- 装完自查:**按品种×来源看区间**。要看的不是「有没有行」，是**同一个品种的两个
-- 来源有没有重叠的日子**——重叠日会让不做来源去重的下游把同一个会员算两遍。
select instrument as 品种, source as 来源, min(trade_date) as 起, max(trade_date) as 止,
       count(*) as 行数
  from seat_history
 where exchange = 'DCE'
 group by 1, 2
 order by 1, 2;

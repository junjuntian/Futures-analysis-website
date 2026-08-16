-- 把三禾采到的大商所席位历史灌进 seat_history。
--
-- 数据来路：交易所自己不给焦煤/鸡蛋/生猪的席位历史（脚本 412、真实浏览器 500），
-- 东财只到 2025-11 且残缺。三禾是唯一覆盖到 2023-08 的来源，按会员逐日采集，
-- 见 `backfill/sanhe_seats.py` 与 `backfill/sanhe_to_csv.py`。
--
-- **rank 一律为空**：三禾按会员组织，给的是该会员的真实持仓，比交易所的「前 20」
-- 更全，代价是没有名次。空着是如实，不编。

\set ON_ERROR_STOP on

begin;

create temp table sanhe_stage (like seat_history);
alter table sanhe_stage drop column id, drop column workspace_id, drop column loaded_at,
  drop column updated_at;

\copy sanhe_stage from '/tmp/seat_dce.csv' with (format csv, header true, null '')

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
      -- 判据用「有没有 market_prices」：每日采集以运营者的账号登录写入，
      -- 有行情的那个空间必然是他在用的。
      select m.workspace_id as id
        from market_prices m
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

select instrument as 品种, source as 来源, min(trade_date) as 起, max(trade_date) as 止,
       count(*) as 行数
  from seat_history
 where exchange = 'DCE'
 group by 1, 2
 order by 1, 2;

-- 从官方龙虎榜反推「掉榜前一日」的真实持仓。
--
-- 交易所只公布前二十名。某会员掉出前二十的那些天，官方文件里根本没有他那一行——
-- 而「掉出前二十」和「真的清仓了」是两件完全不同的事，趋势跟随策略分不清这两者
-- 就会把一次排名滑落读成清仓离场。
--
-- 有一天是能算出来的：**交易所的增减量是相对该会员昨日的真实全量仓算的，不是相对
-- 榜上显示的数**。所以某会员今天回到榜上、昨天不在榜上时：
--
--     昨日真实持仓 = 今日持仓 − 今日增减
--
-- 2026-07-29 的实例：高盛 AU2610 多头 07-28 在榜 2038、07-29 掉榜、07-30 回榜
-- 2416 且增减 +725。若交易所按「昨日为 0」算，增减该是 +2416；实际是 +725，
-- 所以交易所知道昨天真实是 1691。反推得 2416 − 725 = 1691。
--
-- **这是数学上的极限，只能往回推一天。** 掉榜段更早的日子（07-29 之前若还有）
-- 需要 07-29 当天的增减，而那天没有行。推不出来的日子**不写行**——
-- 缺行的含义是「不知道」，而写一个 0 是断言「他清仓了」。
-- 三禾就是在这里把 0 当成事实填进去的，我们不跟。
--
-- 只从官方源反推。三禾那一路的增减字段本身就是「清零差分」（掉榜日显示
-- −前日持仓），拿它去反推是把错误再乘一遍。
--
-- 幂等：先删本次窗口内自己写过的行，再重算。

\set ON_ERROR_STOP on
\if :{?window_days}
\else
\set window_days 7
\endif

begin;

delete from seat_history
 where source = 'reboard_inferred'
   and trade_date >= current_date - :window_days;

with official as (
    select workspace_id, exchange, instrument, contract, rank_type, member,
           trade_date, quantity, change
      from seat_history
     where not is_variety_total and contract is not null
       and source like '%\_official' and change is not null
),
-- 合约的交易日历：官方那天发过这个合约，就说明那天有交易。用它找「前一个交易日」，
-- 而不是 trade_date - 1：中间隔着周末与长假，减一天会指到一个根本没有数据的日子。
calendar as (
    select distinct workspace_id, exchange, instrument, contract, trade_date
      from official
),
with_prev as (
    select c.*,
           lag(trade_date) over (partition by workspace_id, exchange, instrument, contract
                                 order by trade_date) prev_date
      from calendar c
),
inferred as (
    select o.workspace_id, o.exchange, o.instrument, o.contract, o.rank_type, o.member,
           p.prev_date trade_date, o.quantity - o.change quantity
      from official o
      join with_prev p
        on p.workspace_id = o.workspace_id and p.exchange = o.exchange
       and p.instrument = o.instrument and p.contract = o.contract
       and p.trade_date = o.trade_date
     where p.prev_date is not null
       and p.prev_date >= current_date - :window_days
       -- 昨天没有他这一行，才需要反推；有的话官方数就是真值。
       and not exists (
           select 1 from official x
            where x.workspace_id = o.workspace_id and x.exchange = o.exchange
              and x.instrument = o.instrument and x.contract = o.contract
              and x.rank_type = o.rank_type and x.member = o.member
              and x.trade_date = p.prev_date)
)
insert into seat_history (
    id, workspace_id, exchange, instrument, contract, is_variety_total,
    variety_total_is_computed, trade_date, rank_type, rank, member, quantity, change, source)
select gen_random_uuid(), i.workspace_id, i.exchange, i.instrument, i.contract,
       false, false, i.trade_date, i.rank_type,
       -- 名次留空：他那天本来就不在榜上，编一个名次是凭空捏造。
       null, i.member, i.quantity,
       -- 增减只在**再前一天也知道**时才算得出来。算不出就留空——
       -- 留空是「不知道」，填 0 是「没变化」，这两件事差得很远。
       (select i.quantity - x.quantity
          from seat_history x
         where x.workspace_id = i.workspace_id and x.exchange = i.exchange
           and x.instrument = i.instrument and x.contract = i.contract
           and x.rank_type = i.rank_type and x.member = i.member
           and not x.is_variety_total and x.source like '%\_official'
           and x.trade_date = (
               select max(c.trade_date) from calendar c
                where c.workspace_id = i.workspace_id and c.exchange = i.exchange
                  and c.instrument = i.instrument and c.contract = i.contract
                  and c.trade_date < i.trade_date)),
       'reboard_inferred'
  from inferred i
 where i.quantity >= 0  -- 负持仓说明这一组数据本身有问题，宁可不写
on conflict (workspace_id, trade_date, exchange, instrument, contract,
             is_variety_total, rank_type, member, source) do update set
    quantity = excluded.quantity, change = excluded.change, loaded_at = now();

commit;

-- 落地核对。反推行数为 0 而窗口里明明有官方数据，多半是日历那一段写错了。
select trade_date 交易日, instrument 品种, count(*) 反推行数,
       count(change) 连增减也算出来的, min(quantity) 最小持仓
  from seat_history
 where source = 'reboard_inferred' and trade_date >= current_date - :window_days
 group by 1, 2 order by 1 desc, 2;

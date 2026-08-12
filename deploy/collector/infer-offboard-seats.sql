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
-- **这条恒等式已在生产上验证**：把官方数据按会员+合约+榜别排序，相邻交易日
-- （日期差 1 天）的 267,373 个样本里，`持仓 − 增减 = 前一日持仓` 成立 267,373 次，
-- 100.000%，零例外。而日期差 2 天的样本只有 2% 成立——正因为中间那个交易日他
-- 掉榜了，前值不是真值。这反过来划出了公式的适用边界，恰好就是反推要用的场景。
--
-- **这是数学上的极限，只能往回推一天。** 掉榜段更早的日子需要那天的增减，而那天
-- 没有行。推不出来的日子**不写行**——缺行的含义是「不知道」，写一个 0 是断言
-- 「他清仓了」。三禾就是在这里把 0 当成事实填进去的，我们不跟。
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

-- 全部用窗口函数，不用相关子查询。
--
-- 第一版是 `not exists (select 1 from official where ... trade_date = prev_date)`，
-- 在几百万行的 CTE 上对每一候选行各查一次，生产上跑了一小时没出来。
-- 「昨天他在不在榜上」这件事，用他自己序列的 lag 一次算完就够了。
with official as (
    select workspace_id, exchange, instrument, contract, rank_type, member,
           trade_date, quantity, change
      from seat_history
     where not is_variety_total and contract is not null
       and source like '%\_official' and change is not null
),
-- 合约的交易日历：官方那天发过这个合约，就说明那天有交易。必须用它找「前一个
-- 交易日」，不能用 trade_date - 1：中间隔着周末与长假，减一天会指到一个根本
-- 没有数据的日子。
calendar as (
    select distinct workspace_id, exchange, instrument, contract, trade_date
      from official
),
calendar_prev as (
    select c.*,
           lag(trade_date) over (partition by workspace_id, exchange, instrument, contract
                                 order by trade_date) prev_date
      from calendar c
),
-- 每个会员在这条合约+榜别上，自己上一次出现在榜上是哪天。
member_prev as (
    select o.*,
           lag(trade_date) over (partition by workspace_id, exchange, instrument,
                                              contract, rank_type, member
                                 order by trade_date) own_prev
      from official o
),
inferred as (
    select m.workspace_id, m.exchange, m.instrument, m.contract, m.rank_type, m.member,
           c.prev_date trade_date, m.quantity - m.change quantity
      from member_prev m
      join calendar_prev c
        on c.workspace_id = m.workspace_id and c.exchange = m.exchange
       and c.instrument = m.instrument and c.contract = m.contract
       and c.trade_date = m.trade_date
     where c.prev_date is not null
       and c.prev_date >= current_date - :window_days
       -- 他自己上一次上榜早于「前一个交易日」，说明前一个交易日他不在榜上：
       -- 那天才需要反推。own_prev 为空表示这是他第一次出现，同样需要。
       and (m.own_prev is null or m.own_prev < c.prev_date)
)
insert into seat_history (
    id, workspace_id, exchange, instrument, contract, is_variety_total,
    variety_total_is_computed, trade_date, rank_type, rank, member, quantity, change, source)
select gen_random_uuid(), i.workspace_id, i.exchange, i.instrument, i.contract,
       false, false, i.trade_date, i.rank_type,
       -- 名次留空：他那天本来就不在榜上，编一个名次是凭空捏造。
       null, i.member, i.quantity,
       -- 增减留空。要算它得知道再前一天的持仓，而那天多半也不在榜上；留空是
       -- 「不知道」，填 0 是「没变化」——三禾的 1691 配 −2038 就是不肯留空的结果。
       null, 'reboard_inferred'
  from inferred i
 where i.quantity >= 0  -- 负持仓说明这一组数据本身有问题，宁可不写
on conflict (workspace_id, trade_date, exchange, instrument, contract,
             is_variety_total, rank_type, member, source) do update set
    quantity = excluded.quantity, loaded_at = now();

commit;

-- 落地核对。反推行数为 0 而窗口里明明有官方数据，多半是日历那一段写错了。
select instrument 品种, count(*) 反推行数,
       min(trade_date) 起, max(trade_date) 止, min(quantity) 最小持仓
  from seat_history
 where source = 'reboard_inferred'
 group by 1 order by 1;

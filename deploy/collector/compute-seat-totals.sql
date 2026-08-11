-- 给没有官方品种汇总的交易所自算汇总行。
--
-- 建仓过程的「品种汇总」档读 is_variety_total = true 的行。三家里只有郑商所官方发
-- 汇总；大商所（三禾/东财/akshare 全是逐合约）和上期所（全史逐合约）一行汇总都
-- 没有——那两家的品种汇总图一直是空的。两张表设计时就预留了
-- variety_total_is_computed 标志，官方发的与我们自算的必须分得出来：这里补的是后者。
--
-- 口径：同一 (会员原文, 品种, 日, 榜, 来源) 内对各合约求和。change 同样求和——
-- 交易所公布的增减本就是逐合约的，把它们加起来是聚合已公布数字，不是造数。
-- rank 留空：名次属于逐合约榜，汇总没有名次。
--
-- 幂等：窗口内先删自算行再重建。不用 upsert——迁移 202608110002 之前 NULL 合约
-- 绕过唯一约束，upsert 对汇总行从不触发，正是重复行的来路。
--
-- 只补「该 (交易所, 品种, 日, 榜, 会员) 没有任何官方汇总」的组合：郑商所有官方
-- 汇总，再叠一份自算的等于同一个事实两种数字。

\set ON_ERROR_STOP on
\set window_days 3700

begin;

delete from seat_history
 where is_variety_total
   and variety_total_is_computed
   and trade_date >= current_date - :window_days;

insert into seat_history (
    id, workspace_id, exchange, instrument, contract, is_variety_total,
    variety_total_is_computed, trade_date, rank_type, rank, member, quantity, change, source
)
select gen_random_uuid(), s.workspace_id, s.exchange, s.instrument,
       null, true, true, s.trade_date, s.rank_type, null, s.member,
       sum(s.quantity), sum(s.change), s.source
  from seat_history s
 where not s.is_variety_total
   and s.trade_date >= current_date - :window_days
   and not exists (
       select 1 from seat_history official
        where official.workspace_id = s.workspace_id
          and official.trade_date = s.trade_date
          and official.exchange = s.exchange
          and official.instrument = s.instrument
          and official.member = s.member
          and official.rank_type = s.rank_type
          and official.is_variety_total
          and not official.variety_total_is_computed
   )
 group by s.workspace_id, s.exchange, s.instrument, s.trade_date,
          s.rank_type, s.member, s.source;

commit;

select exchange, variety_total_is_computed as 自算, count(*) as 汇总行数,
       min(trade_date) as 起, max(trade_date) as 止
  from seat_history where is_variety_total
 group by 1, 2 order by 1, 2;

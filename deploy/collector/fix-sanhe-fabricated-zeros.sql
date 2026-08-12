-- 修掉三禾填出来的「持仓 0」。
--
-- 三禾不是全量源。它把交易所前二十榜重构了一遍：在榜日抄原始数据，掉榜日**填一行
-- 持仓 0、增减记 −前日持仓**，回榜日用交易所的增减反推前一日。运营者 2026-08-12
-- 指出这套手法，生产数据逐条坐实。
--
-- 那个 0 是猜的，不是观测的。生产实例：瑞达期货 JD2609 空头 2026-08-07 三禾记 0，
-- 而 08-10 是 2117 / −56 —— 真值是 2117 − (−56) = 2173，不是 0。
--
-- **0 恰恰是趋势跟随最怕的那个数**：它把「掉出前二十」说成「清仓离场」。库里这样的
-- 行有 17,753 条，全部带着「持仓 0 且增减为负」的指纹，没有一个例外——交易所的前
-- 二十榜不会列出持仓为 0 的会员，所以这些 0 只可能是填的。
--
-- 分两类处理：
--   能修的：下一个交易日该会员回榜了 → 真值 = 回榜持仓 − 回榜增减，改写过去。
--           这套反推已用官方数据在 26.7 万个相邻交易日样本上验证，100% 成立
--           （见 infer-offboard-seats.sql 的说明）。
--   修不了的：下一个交易日他仍不在榜（或再没有下一行）→ **删掉**。
--           缺行的含义是「不知道」，留一个 0 是断言「他清仓了」。宁可少一行。
--
-- 增减一律清空：那个 −前日持仓 是清零差分，配上修正后的持仓就是自相矛盾
-- （三禾的 1691 配 −2038 正是这么来的）。
--
-- 幂等：修过的行不再是 0，第二遍跑什么都不做。

\set ON_ERROR_STOP on

begin;

-- 三禾自己的交易日历：它那天发过这个合约，就说明那天有数据。
create temp table sanhe_calendar as
select distinct workspace_id, exchange, instrument, contract, trade_date
  from seat_history
 where source = 'sanhe' and not is_variety_total and contract is not null;

create temp table sanhe_next as
select c.*,
       lead(trade_date) over (partition by workspace_id, exchange, instrument, contract
                              order by trade_date) next_trading
  from sanhe_calendar c;

-- 每个会员序列里，零持仓行的下一行是什么。
create temp table zero_rows as
select s.id, s.workspace_id, s.exchange, s.instrument, s.contract,
       s.rank_type, s.member, s.trade_date,
       lead(s.quantity)   over m next_q,
       lead(s.change)     over m next_c,
       lead(s.trade_date) over m next_d
  from seat_history s
 where s.source = 'sanhe' and not s.is_variety_total and s.contract is not null
window m as (partition by s.workspace_id, s.exchange, s.instrument, s.contract,
                          s.rank_type, s.member
             order by s.trade_date);

create temp table fixable as
select z.id, (z.next_q - z.next_c) true_quantity
  from zero_rows z
  join seat_history s on s.id = z.id and s.quantity = 0
  join sanhe_next n on n.workspace_id = z.workspace_id and n.exchange = z.exchange
                   and n.instrument = z.instrument and n.contract = z.contract
                   and n.trade_date = z.trade_date
 where z.next_q > 0
   -- 下一行必须正好是下一个交易日：隔了一天以上，反推出来的是那一天的前一日，
   -- 不是这一行的日期。
   and z.next_d = n.next_trading
   and z.next_c is not null
   and (z.next_q - z.next_c) >= 0;

update seat_history s
   set quantity = f.true_quantity,
       -- 增减清空：三禾留下的是清零差分，与修正后的持仓自相矛盾。
       change = null,
       loaded_at = now()
  from fixable f
 where s.id = f.id;

-- 修不了的零持仓行删掉。缺行 = 不知道；留 0 = 断言清仓了。
--
-- **但不能删还没轮到的那些。** 这个脚本进了日更管线，每天跑。最新一天的零持仓行
-- 天然还没有「下一个交易日」可用来反推，按上面的判定就是「修不了」——今天删掉，
-- 明天他回榜、真值本来算得出来时，那一行已经不在了。
-- 所以只删「后面确实已经有交易日、给过机会仍然推不出来」的行。
-- 大商所尤其要紧：官方源那条路是死的，infer-offboard-seats.sql 只认 *_official，
-- 补不上这一刀删掉的东西。
delete from seat_history s
 where s.source = 'sanhe' and not s.is_variety_total and s.quantity = 0
   and not exists (select 1 from fixable f where f.id = s.id)
   and exists (
       select 1 from sanhe_next n
        where n.workspace_id = s.workspace_id and n.exchange = s.exchange
          and n.instrument = s.instrument and n.contract = s.contract
          and n.trade_date = s.trade_date and n.next_trading is not null
   );

commit;

-- 落地核对。剩余零持仓应当只剩两类：反推出来真值恰好为 0 的（change 已清空），
-- 以及最新一天那些还没轮到的。数字持续增长说明上面的判定出了问题。
select '已修正(增减已清空)' as 项, count(*) 行数 from seat_history
 where source = 'sanhe' and change is null and not is_variety_total
union all
select '剩余零持仓', count(*) from seat_history
 where source = 'sanhe' and quantity = 0 and not is_variety_total
union all
select '其中带增减的(应为 0)', count(*) from seat_history
 where source = 'sanhe' and quantity = 0 and not is_variety_total and change is not null;

-- 修掉三禾回写持仓后没跟着改的「增减」。
--
-- 三禾在掉榜日填一行持仓 0、增减记 −前日持仓（清零差分）；等该会员回榜，它用交易所
-- 增减反推出那天的真实持仓，**回头改写掉榜段最后一天的持仓，却没有改增减**。于是留下
-- `1830 (−2623)` 这种自相矛盾的行——运营者 2026-08-12 指出的正是这个痕迹。
--
-- `fix-sanhe-fabricated-zeros.sql` 只管持仓仍是 0 的那批（三禾没来得及回写的）。它回写
-- 过的那批持仓看着正常，问题全在增减字段上，逃过了那一刀。
--
-- 生产实例：财达期货 JD2505 多头
--     04-16  822 (+194)      ← 在榜，交易所原始数据
--     04-17  215 (−822)      ← 掉榜。持仓 215 是三禾反推的，增减 −822 是清零差分
--     04-18  824 (+609)      ← 回榜，交易所原始数据；824 − 609 = 215 印证了持仓没错
-- 真实增减是 215 − 822 = **−607**，不是 −822。
--
-- **这一条比持仓错更要紧**：趋势跟随读的就是增减。持仓 215 是对的，增减却凭空多算了
-- 215 手的减仓。
--
-- 指纹是精确的。全库 33.3 万个相邻交易日样本里：
--     恒等式成立、无指纹        327,675
--     恒等式失败、带指纹          5,317
--     恒等式失败、无指纹              9   ← 0.003%，成因不明，不动它
-- 也就是说排除带指纹的行之后，三禾在榜日的增减 99.997% 复现交易所恒等式
-- （`持仓 − 增减 = 前一日持仓`），与官方源一个水准。这也是 infer-offboard-seats.sql
-- 敢把大商所接进来的依据。
--
-- 修法不是猜：真实增减 = 今日持仓 − 昨日持仓，两个持仓都在库里且都已印证，这是算术，
-- 不是造数。昨天他也不在榜上（没有行）就算不出来，那种留空——`fix-sanhe-fabricated-zeros.sql`
-- 修过的行增减本来就是空的。
--
-- 幂等：改完 `增减 = −昨持仓` 不再成立（除非今日持仓为 0，而那种行归零持仓脚本管），
-- 第二遍跑什么都不做。

\set ON_ERROR_STOP on

begin;

-- 三禾自己的交易日历。必须用它找「前一个交易日」，不能用 trade_date - 1：
-- 中间隔着周末与长假，减一天会指到一个根本没有数据的日子。
create temp table sanhe_calendar as
select workspace_id, exchange, instrument, contract, trade_date,
       lag(trade_date) over (partition by workspace_id, exchange, instrument, contract
                             order by trade_date) prev_trading
  from (select distinct workspace_id, exchange, instrument, contract, trade_date
          from seat_history
         where source = 'sanhe' and not is_variety_total and contract is not null) d;

create index sanhe_calendar_key on sanhe_calendar (workspace_id, contract, trade_date);
analyze sanhe_calendar;

create temp table seq as
select id, workspace_id, exchange, instrument, contract, rank_type, member, trade_date,
       quantity, change,
       lag(quantity)   over m prev_q,
       lag(trade_date) over m prev_d
  from seat_history
 where source = 'sanhe' and not is_variety_total and contract is not null
   and change is not null
window m as (partition by workspace_id, exchange, instrument, contract, rank_type, member
             order by trade_date);

create temp table rewritten as
select s.id, s.quantity - s.prev_q true_change
  from seq s
  join sanhe_calendar c
    on c.workspace_id = s.workspace_id and c.exchange = s.exchange
   and c.instrument = s.instrument and c.contract = s.contract
   and c.trade_date = s.trade_date
 where s.prev_q is not null
   -- 昨天他也有行，差分才算得出来。
   and s.prev_d = c.prev_trading
   -- 清零差分指纹。
   and s.change = -s.prev_q
   -- 恒等式确实不成立才动它：万一某天真的从 prev_q 减到 0 再无变化，
   -- 增减恰好等于 −prev_q 而且是对的，那种行不该碰。
   and s.quantity - s.change <> s.prev_q
   -- 持仓仍是 0 的行归 fix-sanhe-fabricated-zeros.sql 管，别两个脚本抢同一行。
   and s.quantity <> 0;

update seat_history s
   set change = r.true_change,
       loaded_at = now()
  from rewritten r
 where s.id = r.id;

commit;

-- 落地核对。修完之后带指纹的行应当归零；不归零说明上面某个条件写窄了。
with seq as (
  select instrument, contract, rank_type, member, trade_date, quantity, change,
         lag(quantity)   over m prev_q,
         lag(trade_date) over m prev_d
    from seat_history
   where source = 'sanhe' and not is_variety_total and contract is not null
     and change is not null
  window m as (partition by workspace_id, exchange, instrument, contract, rank_type, member
               order by trade_date)
), cal as (
  select contract, trade_date,
         lag(trade_date) over (partition by contract order by trade_date) prev_trading
    from (select distinct contract, trade_date
            from seat_history
           where source = 'sanhe' and not is_variety_total and contract is not null) d
)
select count(*) 相邻样本,
       count(*) filter (where s.quantity - s.change = s.prev_q) 恒等式成立,
       count(*) filter (where s.change = -s.prev_q
                          and s.quantity - s.change <> s.prev_q) "残留清零差分(应为 0)"
  from seq s
  join cal c on c.contract = s.contract and c.trade_date = s.trade_date
 where s.prev_q is not null and s.prev_d = c.prev_trading;

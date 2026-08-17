-- 套利监控每日快照。跟在投影与品种汇总之后跑，读的是 price_history。
--
-- 只写「位置」不写「触发结论」，理由见迁移 202608120001 的注释：阈值留到读的时候套。
--
-- 幂等：按 (workspace, 交易日, 两条腿) upsert，同一天重复跑结果一致。

\set ON_ERROR_STOP on
\if :{?window_days}
\else
\set window_days 3
\endif

begin;

-- 监控范围与组合规则（运营者 2026-08-12 拍板）：
--   鸡蛋 JD / 生猪 LH / 焦煤 JM / 苹果 AP —— 只和自己跨月
--   玻璃 FG / 纯碱 SA —— 自己跨月，另可互相跨品种但**月份必须一致**
--   黄金 AU / 白银 AG 不做套利（它们在库里是给机构资金模块用的）
-- 腿序照套利页规则：先到期的减后到期的。跨品种同月没有先后，固定玻璃 − 纯碱。
create temp table monitor_scope (instrument text primary key);
insert into monitor_scope values ('JD'), ('LH'), ('JM'), ('AP'), ('FG'), ('SA');

-- 主力月份。运营者 2026-08-12 明确：玻璃、纯碱、焦煤的主力合约是 1、5、9 月，
-- **其余月份不做套利，也不进监控**。非主力月份的合约成交稀疏，价差是几手撮出来的，
-- 报出来也没法交易。
--
-- 只列运营者点名的三个品种。鸡蛋、生猪、苹果没说，就不限制——**猜一个月份清单去
-- 悄悄删掉人家的组合，比多报几组糟得多**。要限制时往这张表里加一行即可。
create temp table main_month (instrument text, mm int);
insert into main_month values
    ('FG', 1), ('FG', 5), ('FG', 9),
    ('SA', 1), ('SA', 5), ('SA', 9),
    ('JM', 1), ('JM', 5), ('JM', 9);

-- **必须按来源去重。** 同一合约同一天在多个源下各有一行（郑商所 08-11 就同时有
-- czce_official 与 akshare_v1），不去重的后果有两个，一个吵一个哑：
--   吵：逐日快照的 insert 会对同一组合同一天产生两行，直接撞 on conflict 报错。
--   哑：旧版有 group by 兜着不报错，但 `pair_days` 被算成两倍——「至少 60 天历史」
--       那道门实际上是按 30 天在放行，而且没人看得出来。
-- 交易所自己发的压过封装源，与席位那边的 SEAT_SOURCE_RANK 同一套道理。
create temp table legs as
select distinct on (p.workspace_id, p.instrument, p.contract, p.trade_date)
       p.workspace_id, p.instrument, p.contract, p.trade_date,
       p.close_price, p.open_interest,
       substring(p.contract from '([0-9]{2})[0-9]{2}$')::int yy,
       substring(p.contract from '[0-9]{2}$')::int mm
  from price_history p
  join monitor_scope s on s.instrument = p.instrument
 where p.close_price is not null
 order by p.workspace_id, p.instrument, p.contract, p.trade_date,
          case when p.source like '%\_official' then 0
               when p.source = 'akshare_v1' then 1
               when p.source like 'eastmoney%' then 2
               when p.source like 'sina%' then 3
               else 4 end,
          p.source;

create index legs_by_contract on legs (workspace_id, contract, trade_date);
create index legs_by_month on legs (workspace_id, instrument, mm, trade_date);
analyze legs;

-- 只监控「当前挂牌且有量」的合约。
--
-- 不加这道门的话，六个品种两两组合是 556 组，其中 319 组会触发——57% 都在报警，
-- 等于没报。噪音全来自刚上市没几天、一天成交几手的远月：历史只有五天，当前价差
-- 当然天天贴着「极值」。取持仓量前 6，是覆盖主力与次主力的经验值。
create temp table liquid as
select l.*
  from (select l.*, row_number() over (partition by l.workspace_id, l.instrument
                     order by l.open_interest desc nulls last) oi_rank
          from legs l
          join (select workspace_id, instrument, max(trade_date) d
                  from legs group by 1, 2) t
            on t.workspace_id = l.workspace_id and t.instrument = l.instrument
           and t.d = l.trade_date) l
 where l.oi_rank <= 6
   -- 主力月份限制。没在 main_month 里列出的品种不受限（见那张表上的注释）。
   and (not exists (select 1 from main_month m where m.instrument = l.instrument)
        or exists (select 1 from main_month m
                    where m.instrument = l.instrument and m.mm = l.mm));

create temp table combo as
select a.workspace_id, a.instrument i1, a.contract c1, a.mm m1, a.yy y1,
       b.instrument i2, b.contract c2, b.mm m2, b.yy y2, false is_cross
  from liquid a
  join liquid b on b.workspace_id = a.workspace_id and b.instrument = a.instrument
 where (a.yy, a.mm) < (b.yy, b.mm)
union all
select a.workspace_id, a.instrument, a.contract, a.mm, a.yy,
       b.instrument, b.contract, b.mm, b.yy, true
  from liquid a
  join liquid b on b.workspace_id = a.workspace_id
 where a.instrument = 'FG' and b.instrument = 'SA'
   and a.yy = b.yy and a.mm = b.mm;

-- 当年：该合约对自身的全部历史，原始最低/最高。
-- 不足 60 天的组合直接不监控——「五天的历史极值」不是极值。
-- 每个组合的完整价差序列，以及**截至当日**的滚动最低/最高。
--
-- 滚动，不是全期极值。这张表是给「触发留记录」用的：要回答「8 月 12 日那天页面上
-- 报了什么」，区间就必须只含 8 月 12 日及之前的数据。用全期极值算出来的历史记录
-- 带着未来信息，拿去回测策略是自欺——那天根本不可能知道后面会出什么极值。
create temp table pair_series as
select k.workspace_id, k.c1, k.c2, x.trade_date,
       (x.close_price - y.close_price) v
  from combo k
  join legs x on x.workspace_id = k.workspace_id and x.contract = k.c1
  join legs y on y.workspace_id = k.workspace_id and y.contract = k.c2
             and y.trade_date = x.trade_date;

create index pair_series_key on pair_series (workspace_id, c1, c2, trade_date);
analyze pair_series;

-- 三层套下来是为了拿到 `prev_pair_pos`：窗口函数不能引用同一层的别名，位置要先
-- 算出来才能对它取 lag。前一日位置是给读时判「段首日」用的（迁移 202608170001）。
create temp table pair_stat as
select y.*,
       lag(y.pair_pos) over (partition by y.workspace_id, y.c1, y.c2
                             order by y.trade_date) prev_pair_pos
  from (select x.*,
               case when x.hi > x.lo and x.days >= 60
                    then (x.now - x.lo) / (x.hi - x.lo) end pair_pos
          from (select workspace_id, c1, c2, trade_date, v now,
                       min(v) over w lo,
                       max(v) over w hi,
                       count(*) over w days
                  from pair_series
                window w as (partition by workspace_id, c1, c2 order by trade_date
                             rows between unbounded preceding and current row)) x) y;

-- 只保留窗口内、且到那天为止已积累够 60 天的行。
-- 60 天的门槛也按当日算：一个组合在它上市第 30 天时，「历史极值」确实还没有意义。
--
-- **窗口比要写入的多留 7 天**：历年轨的前一日位置也得算出来才能判段首日，而历年轨
-- 是只对保留下来的行算的。7 天覆盖得住周末与长假。多出来的行在最后 insert 时按
-- `:window_days` 过滤掉，不会写进表。
delete from pair_stat
 where days < 60
    or trade_date < current_date - ((:window_days + 7) || ' days')::interval;

create index pair_stat_key on pair_stat (workspace_id, c1, c2, trade_date);
analyze pair_stat;

-- 历年：同月份组合在所有年份上的第 2.5 / 97.5 百分位，**同样截至当日**。
--
-- percentile_cont 是有序集聚合，不能当窗口函数用，所以这里对每个 (组合, 日期)
-- 各算一次。看着贵，实际不贵：日更窗口只有三天，组合数几十个，一共百来次。
-- 去极端值的理由见迁移 202608120001 的注释（苹果历年最低 −10686 会把区间撑到
-- 12536，百分比长期贴在中间不动）。
create temp table years_stat as
select p.workspace_id, p.c1, p.c2, p.trade_date, y.days, y.lo, y.hi
  from pair_stat p
  join combo k on k.workspace_id = p.workspace_id and k.c1 = p.c1 and k.c2 = p.c2
  cross join lateral (
      select count(*) days,
             percentile_cont(0.025) within group (order by x.close_price - y2.close_price) lo,
             percentile_cont(0.975) within group (order by x.close_price - y2.close_price) hi
        from legs x
        join legs y2 on y2.workspace_id = x.workspace_id and y2.instrument = k.i2
                    and y2.trade_date = x.trade_date and y2.mm = k.m2
                    and y2.yy = x.yy + (k.y2 - k.y1)
       where x.workspace_id = k.workspace_id and x.instrument = k.i1 and x.mm = k.m1
         and x.trade_date <= p.trade_date
  ) y;

create index years_stat_key on years_stat (workspace_id, c1, c2, trade_date);
analyze years_stat;

-- 两条轨的位置与各自的前一日值汇到一张表，insert 从这里取。
-- 历年轨的 lag 必须在这里做：years_stat 只对保留下来的行算，而 pair_stat 已经按
-- 「窗口 + 7 天」多留了几天，正好够 lag 取到前一交易日。
create temp table snap as
select s.*,
       lag(s.years_pos) over (partition by s.workspace_id, s.c1, s.c2
                              order by s.trade_date) prev_years_pos
  from (select p.workspace_id, p.c1, p.c2, p.trade_date, p.now, p.days,
               p.lo, p.hi, p.pair_pos, p.prev_pair_pos,
               y.days years_days, y.lo years_lo, y.hi years_hi,
               case when y.hi > y.lo then (p.now - y.lo) / (y.hi - y.lo) end years_pos
          from pair_stat p
          left join years_stat y on y.workspace_id = p.workspace_id
                                and y.c1 = p.c1 and y.c2 = p.c2
                                and y.trade_date = p.trade_date) s;

-- ---------------------------------------------------------------------------
-- 历史回归率。口径的完整理由写在迁移 202608170001 里，这里只记要点：
--   · 主体是**月份组合模板**（同品种 + 同月份对 + 同年差），不是具体合约对——
--     一个具体合约对一辈子只有一个生命周期，极值段两三段，算不出有意义的比率。
--   · 极值段按**当年轨**划分，不用页面报警的合成轨:合成轨要先有历年百分位，而
--     历年轨是逐 (组合,日期) 跑 percentile_cont 的，推到全历史会从百来次变成几万次。
--     这是工程折中，页面必须标注口径。
--   · 段首日起 20 个交易日，价差朝该回归的方向走即算命中。
-- ---------------------------------------------------------------------------

create temp table template as
select distinct i1, m1, i2, m2, (y2 - y1) ydiff from combo;

-- 模板下所有年份的实例，不限于当前挂牌的那几个合约。
create temp table hist_pair as
select t.i1, t.m1, t.i2, t.m2, t.ydiff,
       a.workspace_id, a.contract c1, b.contract c2, a.trade_date,
       (a.close_price - b.close_price) v
  from template t
  join legs a on a.instrument = t.i1 and a.mm = t.m1
  join legs b on b.workspace_id = a.workspace_id
             and b.instrument = t.i2 and b.mm = t.m2
             and b.yy = a.yy + t.ydiff
             and b.trade_date = a.trade_date;

create index hist_pair_key on hist_pair (workspace_id, c1, c2, trade_date);
analyze hist_pair;

-- 滚动区间位置与 20 交易日后的价差。
--
-- `lead` 取了未来，但它是**被统计的结果**，不是判定触发的输入：位置仍然只用截至
-- 当日的滚动极值算。两者混在一起才是未来函数，分开就不是。不足 20 日的段（最近
-- 发生的那些）v20 为空，自然不计入——它们的结果还没发生，统计因此永远落后 20 个
-- 交易日，这是对的。
create temp table hist_pos as
select y.*,
       lag(y.pos) over (partition by y.workspace_id, y.c1, y.c2
                        order by y.trade_date) prev_pos
  from (select x.i1, x.m1, x.i2, x.m2, x.ydiff, x.workspace_id, x.c1, x.c2,
               x.trade_date, x.v, x.v20,
               case when x.hi > x.lo and x.n >= 60
                    then (x.v - x.lo) / (x.hi - x.lo) end pos
          from (select h.*,
                       min(v) over w lo,
                       max(v) over w hi,
                       count(*) over w n,
                       lead(v, 20) over (partition by workspace_id, c1, c2
                                         order by trade_date) v20
                  from hist_pair h
                window w as (partition by workspace_id, c1, c2 order by trade_date
                             rows between unbounded preceding and current row)) x) y;

-- 段首日 = 今天触发、前一天不触发。三档各判一次。
-- `prev_pos is null` 也算段首日：那是「刚攒够 60 天历史就触发」，确实是新进入极值。
create temp table revert_stat as
select i1, m1, i2, m2, ydiff, thr, side,
       count(*)::int n,
       count(*) filter (where hit)::int hit
  from (select h.i1, h.m1, h.i2, h.m2, h.ydiff, t.thr,
               case when h.pos <= t.thr then 'low' else 'high' end side,
               case when h.pos <= t.thr then (h.v20 - h.v) > 0
                    else (h.v20 - h.v) < 0 end hit
          from hist_pos h
          cross join (values (0.03::numeric), (0.05), (0.10)) t(thr)
         where h.pos is not null
           and h.v20 is not null
           and (h.pos <= t.thr or h.pos >= 1 - t.thr)
           and (h.prev_pos is null
                or not (h.prev_pos <= t.thr or h.prev_pos >= 1 - t.thr))) s
 group by 1, 2, 3, 4, 5, 6, 7;

-- 摊平成一行一模板。样本为 0 的档在这里就是 null（filter 没命中 → max 为 null），
-- 与表上「命中与样本必须成对、样本 > 0」的约束一致：0/0 显示成 0% 是最坏的一种错。
create temp table revert_wide as
select i1, m1, i2, m2, ydiff,
       max(hit) filter (where side = 'low'  and thr = 0.03) low_hit_3,
       max(n)   filter (where side = 'low'  and thr = 0.03) low_n_3,
       max(hit) filter (where side = 'low'  and thr = 0.05) low_hit_5,
       max(n)   filter (where side = 'low'  and thr = 0.05) low_n_5,
       max(hit) filter (where side = 'low'  and thr = 0.10) low_hit_10,
       max(n)   filter (where side = 'low'  and thr = 0.10) low_n_10,
       max(hit) filter (where side = 'high' and thr = 0.03) high_hit_3,
       max(n)   filter (where side = 'high' and thr = 0.03) high_n_3,
       max(hit) filter (where side = 'high' and thr = 0.05) high_hit_5,
       max(n)   filter (where side = 'high' and thr = 0.05) high_n_5,
       max(hit) filter (where side = 'high' and thr = 0.10) high_hit_10,
       max(n)   filter (where side = 'high' and thr = 0.10) high_n_10
  from revert_stat
 group by 1, 2, 3, 4, 5;

analyze revert_wide;

-- 先清掉窗口内的旧快照，再重算。
--
-- 只做 upsert 是不够的：**监控范围变小时，upsert 不会删掉已经不该存在的行。**
-- 2026-08-12 实证——当天 09:30 的日更用的还是加主力月份限制之前的脚本，写进了
-- FG2703、FG2704 这些非主力月的组合；部署后重算，新脚本不再产出它们，upsert
-- 自然也不会碰它们，于是那些组合就一直挂在页面上，而且看不出是陈的。
-- 品种汇总脚本早就有这么一句 delete，这里当初漏了。
delete from spread_monitor_daily
 where trade_date >= current_date - (:window_days || ' days')::interval;

-- `where` 那一行把「为算 lag 多留的 7 天」挡在表外——多留是手段，不是要写进去的
-- 数据。漏掉它会把窗口外的旧行也重写一遍，看不出错但白做功。
insert into spread_monitor_daily (
    id, workspace_id, trade_date, instrument_1, contract_1, instrument_2, contract_2,
    is_cross_variety, spread, pair_days, pair_low, pair_high, pair_position,
    years_days, years_low, years_high, years_position,
    prev_pair_position, prev_years_position,
    revert_low_hit_3, revert_low_n_3, revert_low_hit_5, revert_low_n_5,
    revert_low_hit_10, revert_low_n_10,
    revert_high_hit_3, revert_high_n_3, revert_high_hit_5, revert_high_n_5,
    revert_high_hit_10, revert_high_n_10)
select gen_random_uuid(), s.workspace_id, s.trade_date,
       k.i1, k.c1, k.i2, k.c2, k.is_cross, s.now,
       s.days, s.lo, s.hi, s.pair_pos,
       s.years_days, s.years_lo, s.years_hi, s.years_pos,
       s.prev_pair_pos, s.prev_years_pos,
       r.low_hit_3, r.low_n_3, r.low_hit_5, r.low_n_5, r.low_hit_10, r.low_n_10,
       r.high_hit_3, r.high_n_3, r.high_hit_5, r.high_n_5, r.high_hit_10, r.high_n_10
  from snap s
  join combo k on k.workspace_id = s.workspace_id and k.c1 = s.c1 and k.c2 = s.c2
  left join revert_wide r on r.i1 = k.i1 and r.m1 = k.m1
                         and r.i2 = k.i2 and r.m2 = k.m2
                         and r.ydiff = (k.y2 - k.y1)
 where s.trade_date >= current_date - (:window_days || ' days')::interval
on conflict (workspace_id, trade_date, contract_1, contract_2) do update set
    spread = excluded.spread,
    pair_days = excluded.pair_days, pair_low = excluded.pair_low,
    pair_high = excluded.pair_high, pair_position = excluded.pair_position,
    years_days = excluded.years_days, years_low = excluded.years_low,
    years_high = excluded.years_high, years_position = excluded.years_position,
    prev_pair_position = excluded.prev_pair_position,
    prev_years_position = excluded.prev_years_position,
    revert_low_hit_3 = excluded.revert_low_hit_3,
    revert_low_n_3 = excluded.revert_low_n_3,
    revert_low_hit_5 = excluded.revert_low_hit_5,
    revert_low_n_5 = excluded.revert_low_n_5,
    revert_low_hit_10 = excluded.revert_low_hit_10,
    revert_low_n_10 = excluded.revert_low_n_10,
    revert_high_hit_3 = excluded.revert_high_hit_3,
    revert_high_n_3 = excluded.revert_high_n_3,
    revert_high_hit_5 = excluded.revert_high_hit_5,
    revert_high_n_5 = excluded.revert_high_n_5,
    revert_high_hit_10 = excluded.revert_high_hit_10,
    revert_high_n_10 = excluded.revert_high_n_10,
    computed_at = now();

commit;

-- 落地核对。位置越界（历年轨允许超出 [0,1]，但不该离谱）在这里就该看得出来。
select trade_date 交易日, count(*) 组合数,
       count(*) filter (where pair_position >= 0.9 or pair_position <= 0.1) 当年触发,
       count(*) filter (where years_position >= 0.9 or years_position <= 0.1) 历年触发,
       round(min(least(pair_position, years_position)), 3) 最低位置,
       round(max(greatest(pair_position, years_position)), 3) 最高位置,
       -- 前一日位置的覆盖率:全空说明 lag 没生效(多留 7 天那步被改坏了),
       -- 段首日标记会整片消失,而页面上只是「没有新触发」,看不出是坏了。
       count(*) filter (where prev_pair_position is not null) 有前值,
       count(*) filter (where revert_low_n_3 is not null
                           or revert_high_n_3 is not null) 有回归率
  from spread_monitor_daily
 group by 1 order by 1 desc limit 5;

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

create temp table legs as
select p.workspace_id, p.instrument, p.contract, p.trade_date,
       p.close_price, p.open_interest,
       substring(p.contract from '([0-9]{2})[0-9]{2}$')::int yy,
       substring(p.contract from '[0-9]{2}$')::int mm
  from price_history p
  join monitor_scope s on s.instrument = p.instrument
 where p.close_price is not null;

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
create temp table pair_stat as
select k.workspace_id, k.c1, k.c2, count(*) days,
       min(x.close_price - y.close_price) lo,
       max(x.close_price - y.close_price) hi,
       (array_agg(x.close_price - y.close_price order by x.trade_date desc))[1] now,
       max(x.trade_date) trade_date
  from combo k
  join legs x on x.workspace_id = k.workspace_id and x.contract = k.c1
  join legs y on y.workspace_id = k.workspace_id and y.contract = k.c2
             and y.trade_date = x.trade_date
 group by 1, 2, 3
having count(*) >= 60;

-- 历年：同月份组合在所有年份上的第 2.5 / 97.5 百分位。去极端值的理由见迁移注释。
--
-- **这一步是整段的瓶颈：生产实测 75.7 秒**（2026-08-12 回滚式预演，全部 workspace，
-- legs 15.9 万行）。整段合计约 77 秒。放在每日批里可以接受——采集本身就要十几分钟；
-- 但它也是这份 SQL 里唯一值得再优化的地方，真要动手先量，别凭直觉改索引。
-- 页面不吃这个代价：API 读的是这张表，不重算。
create temp table years_stat as
select k.workspace_id, k.c1, k.c2, count(*) days,
       percentile_cont(0.025) within group (order by x.close_price - y.close_price) lo,
       percentile_cont(0.975) within group (order by x.close_price - y.close_price) hi
  from combo k
  join legs x on x.workspace_id = k.workspace_id and x.instrument = k.i1 and x.mm = k.m1
  join legs y on y.workspace_id = k.workspace_id and y.instrument = k.i2
             and y.trade_date = x.trade_date and y.mm = k.m2
             and y.yy = x.yy + (k.y2 - k.y1)
 group by 1, 2, 3;

-- 先清掉窗口内的旧快照，再重算。
--
-- 只做 upsert 是不够的：**监控范围变小时，upsert 不会删掉已经不该存在的行。**
-- 2026-08-12 实证——当天 09:30 的日更用的还是加主力月份限制之前的脚本，写进了
-- FG2703、FG2704 这些非主力月的组合；部署后重算，新脚本不再产出它们，upsert
-- 自然也不会碰它们，于是那些组合就一直挂在页面上，而且看不出是陈的。
-- 品种汇总脚本早就有这么一句 delete，这里当初漏了。
delete from spread_monitor_daily
 where trade_date >= current_date - (:window_days || ' days')::interval;

insert into spread_monitor_daily (
    id, workspace_id, trade_date, instrument_1, contract_1, instrument_2, contract_2,
    is_cross_variety, spread, pair_days, pair_low, pair_high, pair_position,
    years_days, years_low, years_high, years_position)
select gen_random_uuid(), p.workspace_id, p.trade_date,
       k.i1, k.c1, k.i2, k.c2, k.is_cross, p.now,
       p.days, p.lo, p.hi,
       case when p.hi > p.lo then (p.now - p.lo) / (p.hi - p.lo) end,
       y.days, y.lo, y.hi,
       case when y.hi > y.lo then (p.now - y.lo) / (y.hi - y.lo) end
  from pair_stat p
  join combo k on k.workspace_id = p.workspace_id and k.c1 = p.c1 and k.c2 = p.c2
  left join years_stat y on y.workspace_id = p.workspace_id and y.c1 = p.c1 and y.c2 = p.c2
 where p.trade_date >= current_date - (:window_days || ' days')::interval
on conflict (workspace_id, trade_date, contract_1, contract_2) do update set
    spread = excluded.spread,
    pair_days = excluded.pair_days, pair_low = excluded.pair_low,
    pair_high = excluded.pair_high, pair_position = excluded.pair_position,
    years_days = excluded.years_days, years_low = excluded.years_low,
    years_high = excluded.years_high, years_position = excluded.years_position,
    computed_at = now();

commit;

-- 落地核对。位置越界（历年轨允许超出 [0,1]，但不该离谱）在这里就该看得出来。
select trade_date 交易日, count(*) 组合数,
       count(*) filter (where pair_position >= 0.9 or pair_position <= 0.1) 当年触发,
       count(*) filter (where years_position >= 0.9 or years_position <= 0.1) 历年触发,
       round(min(least(pair_position, years_position)), 3) 最低位置,
       round(max(greatest(pair_position, years_position)), 3) 最高位置
  from spread_monitor_daily
 group by 1 order by 1 desc limit 5;

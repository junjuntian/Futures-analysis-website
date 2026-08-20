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

-- 主力月份。运营者 2026-08-12 明确：玻璃、纯碱、焦煤的主力合约是 1、5、9 月。
-- 非主力月份的合约成交稀疏，价差是几手撮出来的，报出来也没法交易。
--
-- `pair_mode` 定这道门有多严（2026-08-18 运营者拍板把焦煤放宽）：
--   both   —— 两条腿都必须是主力月（玻璃、纯碱：原规则不变）
--   either —— **只要有一条腿是主力月就成对**（焦煤：主力对非主力也做套利）。
--             非主力那条腿仍受持仓量前 6 的流动性门约束，不会把冷门远月放进来。
--
-- 只列运营者点名的三个品种。鸡蛋、生猪、苹果没说，就不限制——**猜一个月份清单去
-- 悄悄删掉人家的组合，比多报几组糟得多**。要限制时往这张表里加一行即可。
create temp table main_month (instrument text, mm int, pair_mode text);
insert into main_month values
    ('FG', 1, 'both'), ('FG', 5, 'both'), ('FG', 9, 'both'),
    ('SA', 1, 'both'), ('SA', 5, 'both'), ('SA', 9, 'both'),
    ('JM', 1, 'either'), ('JM', 5, 'either'), ('JM', 9, 'either');

-- **必须按来源去重。** 同一合约同一天在多个源下各有一行（郑商所 08-11 就同时有
-- czce_official 与 akshare_v1），不去重的后果有两个，一个吵一个哑：
--   吵：逐日快照的 insert 会对同一组合同一天产生两行，直接撞 on conflict 报错。
--   哑：旧版有 group by 兜着不报错，但 `pair_days` 被算成两倍——「至少 60 天历史」
--       那道门实际上是按 30 天在放行，而且没人看得出来。
-- 交易所自己发的压过封装源，与席位那边的 SEAT_SOURCE_RANK 同一套道理。
-- **收盘价 0 = 当天无成交,不是价格**(2026-08-18 做基差探针时挖出)。
-- 郑商所对无成交合约的收盘价如实写 0,结算价照常有效:AP2111 在 2021-11-03
-- 收盘 0 / 结算 7525,而 `close_price is not null` 只挡 NULL 不挡 0,于是
-- AP2111−AP2112 那天的价差被算成 0 − 8020 = **−8020**(前后两天的真实价差是
-- −363 与 −102)。这个假极值会永久钉死该组合区间的下界,造出假的低位报警、
-- 假拐头,还污染历年百分位——实测 79 个组合 200 行中招。
-- 修法:无成交日改用结算价(交易所在无成交时本就以结算价为当日代表价),
-- 两者都无效才丢弃。**取价只在这一处,改这里全链路生效。**
create temp table legs as
select distinct on (p.workspace_id, p.instrument, p.contract, p.trade_date)
       p.workspace_id, p.instrument, p.contract, p.trade_date,
       coalesce(nullif(p.close_price, 0), p.settlement_price) close_price,
       p.open_interest,
       substring(p.contract from '([0-9]{2})[0-9]{2}$')::int yy,
       substring(p.contract from '[0-9]{2}$')::int mm
  from price_history p
  join monitor_scope s on s.instrument = p.instrument
 where coalesce(nullif(p.close_price, 0), p.settlement_price) > 0
 order by p.workspace_id, p.instrument, p.contract, p.trade_date,
          -- 「凡带 official 的都是交易所自己发的」。原来写的是 like '%\_official'
          -- ——要求以 _official 结尾，而大商所的源叫 dce_official_history，**匹配不上**，
          -- 官方数据反而掉到最后一档（2026-08-17 核对回归率时抓到）。实测影响为零：
          -- 2024 年底前大商所只有这一个源，2025 起是 sina，只有 2026-07-31~08-05
          -- 六天 akshare 与 sina 并存；两种写法跑出来的统计逐字节相同。但郑商所能
          -- 匹配、大商所匹配不上这种不对称迟早会在别处咬人，就手修掉。
          case when p.source like '%official%' then 0
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
   -- **either 模式的品种在这一层不筛月份**：它的非主力腿要留到 combo 里才判，
   -- 在这里砍掉就配不出「主力 × 非主力」那种对了。流动性门(oi_rank<=6)照旧管着。
   and (not exists (select 1 from main_month m where m.instrument = l.instrument)
        or exists (select 1 from main_month m
                    where m.instrument = l.instrument and m.pair_mode = 'either')
        or exists (select 1 from main_month m
                    where m.instrument = l.instrument and m.mm = l.mm));

create temp table combo as
select a.workspace_id, a.instrument i1, a.contract c1, a.mm m1, a.yy y1,
       b.instrument i2, b.contract c2, b.mm m2, b.yy y2, false is_cross
  from liquid a
  join liquid b on b.workspace_id = a.workspace_id and b.instrument = a.instrument
 where (a.yy, a.mm) < (b.yy, b.mm)
   -- either 模式(焦煤,2026-08-18 运营者拍板):至少一条腿是主力月即成对。
   -- both 模式与不受限的品种在 liquid 层就筛完了,这里恒真。
   and (not exists (select 1 from main_month m
                     where m.instrument = a.instrument and m.pair_mode = 'either')
        or exists (select 1 from main_month m
                    where m.instrument = a.instrument and m.mm in (a.mm, b.mm)))
union all
select a.workspace_id, a.instrument, a.contract, a.mm, a.yy,
       b.instrument, b.contract, b.mm, b.yy, true
  from liquid a
  join liquid b on b.workspace_id = a.workspace_id
 where a.instrument = 'FG' and b.instrument = 'SA'
   and a.yy = b.yy and a.mm = b.mm;

-- 历年实例(DEC-071):把当前每个模板(品种+月份对+年差)按同样口径往回展开,
-- **只收已到期的**——先到期腿的交割月已经开始,散户窗口早关了。这样历史信号
-- 视图能看到 JD2509-JD2601 这类过期组合的当年 ⚡,而「当前挂牌且有量」的主力
-- 圈定门对现役组合原样生效(未到期的年轻实例不进来,免得绕过持仓量前 6 的门)。
-- 过期实例只在大窗口全量重算时产出行;日更窗口内它们没有数据,零成本。
insert into combo
select k.workspace_id, k.i1, l1.contract, k.m1, l1.yy,
       k.i2, l2.contract, k.m2, l2.yy, k.is_cross
  from (select distinct workspace_id, i1, m1, i2, m2, y2 - y1 yoff, is_cross
          from combo) k
  join (select distinct workspace_id, instrument, contract, mm, yy from legs) l1
    on l1.workspace_id = k.workspace_id and l1.instrument = k.i1 and l1.mm = k.m1
  join (select distinct workspace_id, instrument, contract, mm, yy from legs) l2
    on l2.workspace_id = k.workspace_id and l2.instrument = k.i2 and l2.mm = k.m2
   and l2.yy = l1.yy + k.yoff
 where least(make_date(2000 + l1.yy, k.m1, 1), make_date(2000 + l2.yy, k.m2, 1))
       <= current_date
   and not exists (select 1 from combo c
                    where c.workspace_id = k.workspace_id
                      and c.c1 = l1.contract and c.c2 = l2.contract);

-- 当年：该合约对自身的全部历史，原始最低/最高。
-- 不足 30 天的组合直接不监控——「五天的历史极值」不是极值。
-- (原为 60 天;2026-08-18 运营者拍板降到 30:生猪 2705 系组合上市 58 天还进不了
-- 监控,等待成本高于年轻区间的噪音成本。30 天仍挡得住上市头几周的假极值。)
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
select w.*,
       -- 近 20 个交易日的拐头穿线次数(迁移 202608170005):「信号差」降级标与
       -- ⚡「只认首次」(==1)的素材。回撤线按品种分档(DEC-070 全量回测:JM 位置
       -- 日抖动全场最高要深线,JD 早进不受罚要浅线,玻纯回归快要浅线),r.line
       -- 与 API 的 turn_retreat() 同值,0.97/0.03 与 TURN_BAND 同值——改一处
       -- 必须同批改另一处并手动重跑重算。
       sum(w.cross_h) over (partition by w.workspace_id, w.c1, w.c2
                            order by w.trade_date
                            rows between 19 preceding and current row)::int
           turn_crosses_high_20,
       sum(w.cross_l) over (partition by w.workspace_id, w.c1, w.c2
                            order by w.trade_date
                            rows between 19 preceding and current row)::int
           turn_crosses_low_20
  from (select z.*,
               case when z.prev_pair_pos > 1 - r.line and z.pair_pos <= 1 - r.line
                     and z.pair_pos_hi20 >= 0.97 then 1 else 0 end cross_h,
               case when z.prev_pair_pos < r.line and z.pair_pos >= r.line
                     and z.pair_pos_lo20 <= 0.03 then 1 else 0 end cross_l
          from (select y.*,
               lag(y.pair_pos) over (partition by y.workspace_id, y.c1, y.c2
                             order by y.trade_date) prev_pair_pos,
       -- 近 20 个交易日(含当日)的位置极值,「已拐头」的事实素材(迁移 202608170004)。
       -- 按行数开窗:位置本来就只在交易日上有值,20 行即 20 个交易日。
       max(y.pair_pos) over (partition by y.workspace_id, y.c1, y.c2
                             order by y.trade_date
                             rows between 19 preceding and current row) pair_pos_hi20,
       min(y.pair_pos) over (partition by y.workspace_id, y.c1, y.c2
                             order by y.trade_date
                             rows between 19 preceding and current row) pair_pos_lo20
  from (select x.*,
               case when x.hi > x.lo and x.days >= 30
                    then (x.now - x.lo) / (x.hi - x.lo) end pair_pos
          from (select workspace_id, c1, c2, trade_date, v now,
                       min(v) over w lo,
                       max(v) over w hi,
                       count(*) over w days
                  from pair_series
                window w as (partition by workspace_id, c1, c2 order by trade_date
                             rows between unbounded preceding and current row)) x) y) z
          cross join lateral (
              -- 分档表与 API turn_retreat() 一一对应(跨品种现只有玻纯)。
              select case
                       when substring(z.c1 from '^[A-Z]+') <> substring(z.c2 from '^[A-Z]+')
                            then case when substring(z.c1 from '^[A-Z]+') = 'FG'
                                       and substring(z.c2 from '^[A-Z]+') = 'SA'
                                      then 0.08 else 0.10 end
                       -- AP 已于 DEC-075 退回默认档(原 0.20 建立在收盘价 0 的
                       -- 脏数据上,清洗后证据消失)。JM 不受那次污染,保持 0.20。
                       when z.c1 like 'JM%' then 0.20
                       when z.c1 like 'JD%' then 0.05
                       else 0.10
                     end as line) r) w;

-- ---------------------------------------------------------------------------
-- 平台位(DEC-095):价差自己走出来的横盘转折位。运营者下单看的就是它,
-- 而页面此前只有区间位置/分位/回归统计,完全没有这一维。
--
-- 口径与它们的来历(参数是拿运营者点名的 −1355 试出来的,不是拍的):
--   · 转折位 = 收盘价差是**前后各 3 个交易日**里的极值。±5/±7 会把 −1355 弄丢,
--     ±2 会把 −1150/−1155/−1160/−1170 拆成四档。
--   · **必须要求整窗 7 行**(cnt = 7):序列两端窗口不全,会造出假转折。
--   · **因果**:t 处的判定要到 t+3 才成立,所以 as_of 那天只能看到 rn+3 ≤ rn_asof
--     的转折位。不加这一条,历史行就带着未来信息——与 pair_series 那段注释同一个
--     道理。代价是最近三天的转折位当天不算数,界面要写「待确认」。
--   · **0.5σ 内**并成一档,**存 lo/hi 区间不存单点**:链式合并会让 −1080/−1115/−1155
--     并出一个跨 75 点的档,只报均值 −1117 是假精度。
--   · 触碰回合 = 收盘落在该档 ±25 点内的独立回合(连续日算一回合)。
--   · 序列用 pair_series(**完整价差历史**),不是写入窗口那一段。
-- 距离、到达概率、哪一档是卖点/止损**全部读时算**,这里只存事实。
-- ---------------------------------------------------------------------------

create temp table ps_rn as
select workspace_id, c1, c2, trade_date, v,
       row_number() over (partition by workspace_id, c1, c2 order by trade_date) rn
  from pair_series;
create index ps_rn_key on ps_rn (workspace_id, c1, c2, trade_date);
analyze ps_rn;

create temp table pivots as
select workspace_id, c1, c2, rn, v
  from (select *, max(v) over w mx, min(v) over w mn, count(*) over w cnt
          from ps_rn
        window w as (partition by workspace_id, c1, c2 order by trade_date
                     rows between 3 preceding and 3 following)) t
 where cnt = 7 and (v = mx or v = mn);

-- 只给要写库的那些行算(多留 7 天与上面同一个理由)。
create temp table shelf_asof as
select workspace_id, c1, c2, trade_date as_of, rn rn_asof
  from ps_rn
 where trade_date >= current_date - ((:window_days + 7) || ' days')::interval;
create index shelf_asof_key on shelf_asof (workspace_id, c1, c2, as_of);
analyze shelf_asof;

-- 日波动:近 20 个交易日价差日变动的样本标准差。读时用来算「距离 ÷ σ√剩余天数」。
create temp table shelf_sigma as
select workspace_id, c1, c2, trade_date as_of,
       stddev_samp(dv) over (partition by workspace_id, c1, c2 order by trade_date
                             rows between 19 preceding and current row) sigma
  from (select workspace_id, c1, c2, trade_date,
               v - lag(v) over (partition by workspace_id, c1, c2 order by trade_date) dv
          from ps_rn) t;
create index shelf_sigma_key on shelf_sigma (workspace_id, c1, c2, as_of);
analyze shelf_sigma;

create temp table shelf as
select workspace_id, c1, c2, as_of,
       min(lvl) lo, max(lvl) hi, round(avg(lvl))::numeric lvl
  from (select *, sum(g) over (partition by workspace_id, c1, c2, as_of
                               order by lvl desc rows unbounded preceding) grp
          from (select v.*,
                       -- **并档阈值按各品种自己的日波动定**(2026-08-20 修,DEC-098):
                       -- 原来写死 50 点,那是在生猪(σ≈94)上试出来的,≈0.5σ。
                       -- 套到焦煤跨月(σ≈16)就是 3.2σ,把 31 个转折位链式并成一档、
                       -- 跨 150 点——十个日波动宽,完全没用。0.5σ 在生猪上仍是 47 点
                       -- (与原来验证过 −1355 的那个 50 几乎一样),在焦煤上是 8 点。
                       case when lag(lvl) over (partition by workspace_id, c1, c2, as_of
                                                order by lvl desc) - lvl > v.band
                            then 1 else 0 end g
                  from (select distinct a.workspace_id, a.c1, a.c2, a.as_of, p.v lvl,
                               coalesce(0.5 * g.sigma, 50) band
                          from shelf_asof a
                          join pivots p on p.workspace_id = a.workspace_id
                                       and p.c1 = a.c1 and p.c2 = a.c2
                                       and p.rn + 3 <= a.rn_asof
                          left join shelf_sigma g on g.workspace_id = a.workspace_id
                                                 and g.c1 = a.c1 and g.c2 = a.c2
                                                 and g.as_of = a.as_of) v) t) u
 group by workspace_id, c1, c2, as_of, grp;

create temp table shelf_touch as
select workspace_id, c1, c2, as_of, lo, hi, lvl,
       count(*) filter (where near and not prev_near) touches
  from (select s.workspace_id, s.c1, s.c2, s.as_of, s.lo, s.hi, s.lvl, p.trade_date,
               -- 触碰带同样按波动定,取并档阈值的一半(0.25σ)。
               abs(p.v - s.lvl) <= coalesce(0.25 * g.sigma, 25) near,
               lag(abs(p.v - s.lvl) <= coalesce(0.25 * g.sigma, 25), 1, false)
                 over (partition by s.workspace_id, s.c1, s.c2, s.as_of, s.lvl
                       order by p.trade_date) prev_near
          from shelf s
          join ps_rn p on p.workspace_id = s.workspace_id and p.c1 = s.c1 and p.c2 = s.c2
                      and p.trade_date <= s.as_of
          left join shelf_sigma g on g.workspace_id = s.workspace_id and g.c1 = s.c1
                                 and g.c2 = s.c2 and g.as_of = s.as_of) x
 group by workspace_id, c1, c2, as_of, lo, hi, lvl;

create temp table shelf_json as
select workspace_id, c1, c2, as_of,
       jsonb_agg(jsonb_build_object('level', lvl, 'lo', lo, 'hi', hi, 'touches', touches)
                 order by lvl desc) shelves
  from shelf_touch group by workspace_id, c1, c2, as_of;

-- 只保留窗口内、且到那天为止已积累够 30 天的行(2026-08-18 由 60 降,见上)。
-- 门槛按当日算：一个组合在它上市第 10 天时,「历史极值」确实还没有意义。
--
-- **窗口比要写入的多留 7 天**：历年轨的前一日位置也得算出来才能判段首日，而历年轨
-- 是只对保留下来的行算的。7 天覆盖得住周末与长假。多出来的行在最后 insert 时按
-- `:window_days` 过滤掉，不会写进表。
delete from pair_stat
 where days < 30
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
               p.pair_pos_hi20, p.pair_pos_lo20,
               p.turn_crosses_high_20, p.turn_crosses_low_20,
               y.days years_days, y.lo years_lo, y.hi years_hi,
               case when y.hi > y.lo then (p.now - y.lo) / (y.hi - y.lo) end years_pos
          from pair_stat p
          left join years_stat y on y.workspace_id = p.workspace_id
                                and y.c1 = p.c1 and y.c2 = p.c2
                                and y.trade_date = p.trade_date) s;

-- ---------------------------------------------------------------------------
-- 历史回归率(完整口径见迁移 202608170002)。要点:
--   · 可交易窗口照 **5A 窗口引擎**(rust/crates/domain/src/spread_analytics.rs),
--     不另立一套:止点 = 先到期那条腿的散户最后交易日 = 交割月前月的最后一个
--     非周末日(那边的 last_weekday_before_delivery)。
--   · 历年按**月-日**对齐,一直看到各自窗口的止点;**曾经触及**即算回归,不比终点
--     ——套利仓在期间任何有利时刻都能平掉。
--   · 只用**已走完**的年份实例(止点晚于最新数据日的一律排除,含当年)。
--   · 存四个数:hit/n(曾经触及的年数)、move(最有利幅度)、drift(持到止点的净
--     变化,已按方向标准化成「正数=朝回归走」)、days(剩余交易日中位)。
--     **单看 hit/n 会骗人**:剩余期一长它就趋近 100%;JD2612/JD2701 回归率 100%
--     而 drift 中位 −166 点,方向是反的。
-- ---------------------------------------------------------------------------

create temp table template as
select distinct i1, m1, i2, m2, (y2 - y1) ydiff from combo;

-- 模板下所有年份实例及其窗口止点。两条腿各算一个散户最后交易日,取先到期那个。
create temp table hist_meta as
select t.i1, t.m1, t.i2, t.m2, t.ydiff, a.contract c1, b.contract c2,
       least(
         (select (e - (case extract(isodow from e) when 6 then 1 when 7 then 2 else 0 end)
                      * interval '1 day')::date
            from (select (date_trunc('month', make_date(2000 + a.yy, t.m1, 1))
                          - interval '1 day')::date e) z1),
         (select (e - (case extract(isodow from e) when 6 then 1 when 7 then 2 else 0 end)
                      * interval '1 day')::date
            from (select (date_trunc('month', make_date(2000 + b.yy, t.m2, 1))
                          - interval '1 day')::date e) z2)
       ) win_end
  from template t
  join (select distinct instrument, contract, yy, mm from legs) a
    on a.instrument = t.i1 and a.mm = t.m1
  join (select distinct instrument, contract, yy, mm from legs) b
    on b.instrument = t.i2 and b.mm = t.m2 and b.yy = a.yy + t.ydiff;

-- 价差序列裁到窗口内,只保留已走完的实例。
create temp table hist_win as
select m.i1, m.m1, m.i2, m.m2, m.ydiff, m.c1, m.c2, m.win_end,
       x.trade_date, (x.close_price - y.close_price) v
  from hist_meta m
  join legs x on x.contract = m.c1
  join legs y on y.workspace_id = x.workspace_id and y.contract = m.c2
             and y.trade_date = x.trade_date
 where x.trade_date <= m.win_end
   and m.win_end <= (select max(trade_date) from legs);

-- 每行带上「从下一行到窗口止点」的前向统计。预算一次,后面找到锚点行直接读,
-- 省得为每个 (当前日 × 实例) 各扫一遍尾巴。
create temp table hist_fwd as
select h.*,
       min(v) over w fmin,
       max(v) over w fmax,
       count(*) over w fdays,
       last_value(v) over w flast,
       min(trade_date) over (partition by c1, c2) win_start
  from hist_win h
window w as (partition by c1, c2 order by trade_date
             rows between 1 following and unbounded following);

create index hist_fwd_key on hist_fwd (c1, c2, trade_date);
analyze hist_fwd;

-- 每个「当前组合-日」× 每个历史实例的锚点日期 = 与当前日同月-日、且落进该实例
-- 窗口的那一天。窗口可能跨年(02-06 这类会跨到次年 1 月),所以窗口起、止两个年份
-- 都试一次;日号按当月天数截断,否则 2-29 碰上平年 make_date 直接报错。
create temp table anchor as
select s.c1 cur_c1, s.c2 cur_c2, s.trade_date cur_date, f.c1, f.c2,
       case when d2.d between f.win_start and f.win_end then d2.d
            when d1.d between f.win_start and f.win_end then d1.d end anchor_date
  from snap s
  join combo k on k.workspace_id = s.workspace_id and k.c1 = s.c1 and k.c2 = s.c2
  join (select distinct c1, c2, i1, m1, i2, m2, ydiff, win_start, win_end
          from hist_fwd) f
    on f.i1 = k.i1 and f.m1 = k.m1 and f.i2 = k.i2 and f.m2 = k.m2
   and f.ydiff = (k.y2 - k.y1)
   and f.c1 <> s.c1
  cross join lateral (
      select make_date(q.y, q.m, least(q.dd, q.mdays)) d from (
        select extract(year from f.win_start)::int y,
               extract(month from s.trade_date)::int m,
               extract(day from s.trade_date)::int dd,
               extract(day from (date_trunc('month',
                   make_date(extract(year from f.win_start)::int,
                             extract(month from s.trade_date)::int, 1))
                   + interval '1 month' - interval '1 day'))::int mdays) q) d1
  cross join lateral (
      select make_date(q.y, q.m, least(q.dd, q.mdays)) d from (
        select extract(year from f.win_end)::int y,
               extract(month from s.trade_date)::int m,
               extract(day from s.trade_date)::int dd,
               extract(day from (date_trunc('month',
                   make_date(extract(year from f.win_end)::int,
                             extract(month from s.trade_date)::int, 1))
                   + interval '1 month' - interval '1 day'))::int mdays) q) d2;

-- 锚点当天(非交易日则顺延到之后第一个交易日)那一行。
create temp table anchor_row as
select distinct on (a.cur_c1, a.cur_c2, a.cur_date, a.c1, a.c2)
       a.cur_c1, a.cur_c2, a.cur_date, a.c1, a.c2,
       f.v p0, f.fmin, f.fmax, f.flast, f.fdays
  from anchor a
  join hist_fwd f on f.c1 = a.c1 and f.c2 = a.c2 and f.trade_date >= a.anchor_date
 where a.anchor_date is not null
 order by a.cur_c1, a.cur_c2, a.cur_date, a.c1, a.c2, f.trade_date;

-- 锚点正好落在窗口最后一天时没有「后续」,那一年不构成样本。
delete from anchor_row where fdays = 0;

create temp table revert_stat as
select cur_c1, cur_c2, cur_date, side,
       count(*)::int n,
       count(*) filter (where hit)::int hit,
       percentile_cont(0.5) within group (order by move) move_med,
       percentile_cont(0.5) within group (order by drift) drift_med,
       -- 历年 MAE:锚点后先朝不利方向走的最大幅度(高位=继续冲高,低位对称)。
       -- 中位=补仓参考,最大=风险预留(DEC-067)。盈亏比分级已回测否决,只留分母。
       percentile_cont(0.5) within group (order by mae) mae_med,
       max(mae) mae_max,
       round(percentile_cont(0.5) within group (order by fdays))::int days_med
  from (select r.cur_c1, r.cur_c2, r.cur_date, s.side, r.fdays,
               case when s.side = 'high' then r.fmin < r.p0 else r.fmax > r.p0 end hit,
               case when s.side = 'high' then r.p0 - r.fmin else r.fmax - r.p0 end move,
               case when s.side = 'high' then r.p0 - r.flast else r.flast - r.p0 end drift,
               greatest(case when s.side = 'high' then r.fmax - r.p0
                             else r.p0 - r.fmin end, 0) mae
          from anchor_row r
          cross join (values ('high'), ('low')) s(side)) x
 group by 1, 2, 3, 4;

create temp table revert_wide as
select cur_c1, cur_c2, cur_date,
       max(hit)       filter (where side = 'high') high_hit,
       max(n)         filter (where side = 'high') high_n,
       max(move_med)  filter (where side = 'high') high_move,
       max(drift_med) filter (where side = 'high') high_drift,
       max(mae_med)   filter (where side = 'high') high_mae,
       max(mae_max)   filter (where side = 'high') high_mae_max,
       max(days_med)  filter (where side = 'high') high_days,
       max(hit)       filter (where side = 'low')  low_hit,
       max(n)         filter (where side = 'low')  low_n,
       max(move_med)  filter (where side = 'low')  low_move,
       max(drift_med) filter (where side = 'low')  low_drift,
       max(mae_med)   filter (where side = 'low')  low_mae,
       max(mae_max)   filter (where side = 'low')  low_mae_max,
       max(days_med)  filter (where side = 'low')  low_days
  from revert_stat
 group by 1, 2, 3;

analyze revert_wide;

-- ---------------------------------------------------------------------------
-- 数据完整性门(2026-08-19 加)。**必须在 delete 之前**。
--
-- 下面那句 delete 是无条件的:数据还没到齐时它照样先把窗口内的旧快照删光,
-- 再用残缺数据重写。2026-08-19 抢早轮(16:00)首次运行就是这么坏的——
-- 那时只有大商所到货(DCE 16:03,郑商所与上期所要等 16:26),于是当天只算出
-- 鸡蛋一个品种,**而且把 08-17、08-18 两天完整的六品种快照一并重写成了五品种**。
-- 页面上的表现是品种下拉里只剩「鸡蛋」,看不出是数据没到还是程序坏了。
--
-- 判据用「两个交易所都到了没有」而不是「品种数够不够」:监控范围横跨大商所
-- (JD/LH/JM)与郑商所(AP/FG/SA),少任何一边算出来的都是残的;而品种数会因为
-- 某个品种真的停牌而正常减少,拿它当判据会误伤。
select (
    count(distinct exchange) filter (where exchange in ('DCE', 'CZCE')) >= 2
) as monitor_data_ok
  from price_history
 where trade_date = (select max(trade_date) from price_history
                      where instrument in ('JD', 'LH', 'JM', 'AP', 'FG', 'SA'))
   and instrument in ('JD', 'LH', 'JM', 'AP', 'FG', 'SA')
\gset

\if :monitor_data_ok

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
    pair_pos_hi20, pair_pos_lo20,
    turn_crosses_high_20, turn_crosses_low_20,
    revert_high_hit, revert_high_n, revert_high_move, revert_high_drift,
    revert_high_mae, revert_high_mae_max, revert_high_days,
    revert_low_hit, revert_low_n, revert_low_move, revert_low_drift,
    revert_low_mae, revert_low_mae_max, revert_low_days,
    shelves, spread_sigma)
select gen_random_uuid(), s.workspace_id, s.trade_date,
       k.i1, k.c1, k.i2, k.c2, k.is_cross, s.now,
       s.days, s.lo, s.hi, s.pair_pos,
       s.years_days, s.years_lo, s.years_hi, s.years_pos,
       s.prev_pair_pos, s.prev_years_pos,
       s.pair_pos_hi20, s.pair_pos_lo20,
       s.turn_crosses_high_20, s.turn_crosses_low_20,
       r.high_hit, r.high_n, r.high_move, r.high_drift,
       r.high_mae, r.high_mae_max, r.high_days,
       r.low_hit, r.low_n, r.low_move, r.low_drift,
       r.low_mae, r.low_mae_max, r.low_days,
       sj.shelves, nullif(sg.sigma, 0)
  from snap s
  join combo k on k.workspace_id = s.workspace_id and k.c1 = s.c1 and k.c2 = s.c2
  left join revert_wide r on r.cur_c1 = s.c1 and r.cur_c2 = s.c2
                         and r.cur_date = s.trade_date
  left join shelf_json sj on sj.workspace_id = s.workspace_id and sj.c1 = s.c1
                         and sj.c2 = s.c2 and sj.as_of = s.trade_date
  left join shelf_sigma sg on sg.workspace_id = s.workspace_id and sg.c1 = s.c1
                          and sg.c2 = s.c2 and sg.as_of = s.trade_date
 where s.trade_date >= current_date - (:window_days || ' days')::interval
on conflict (workspace_id, trade_date, contract_1, contract_2) do update set
    spread = excluded.spread,
    pair_days = excluded.pair_days, pair_low = excluded.pair_low,
    pair_high = excluded.pair_high, pair_position = excluded.pair_position,
    years_days = excluded.years_days, years_low = excluded.years_low,
    years_high = excluded.years_high, years_position = excluded.years_position,
    prev_pair_position = excluded.prev_pair_position,
    prev_years_position = excluded.prev_years_position,
    shelves = excluded.shelves,
    spread_sigma = excluded.spread_sigma,
    pair_pos_hi20 = excluded.pair_pos_hi20,
    pair_pos_lo20 = excluded.pair_pos_lo20,
    turn_crosses_high_20 = excluded.turn_crosses_high_20,
    turn_crosses_low_20 = excluded.turn_crosses_low_20,
    revert_high_hit = excluded.revert_high_hit,
    revert_high_n = excluded.revert_high_n,
    revert_high_move = excluded.revert_high_move,
    revert_high_drift = excluded.revert_high_drift,
    revert_high_mae = excluded.revert_high_mae,
    revert_high_mae_max = excluded.revert_high_mae_max,
    revert_high_days = excluded.revert_high_days,
    revert_low_hit = excluded.revert_low_hit,
    revert_low_n = excluded.revert_low_n,
    revert_low_move = excluded.revert_low_move,
    revert_low_drift = excluded.revert_low_drift,
    revert_low_mae = excluded.revert_low_mae,
    revert_low_mae_max = excluded.revert_low_mae_max,
    revert_low_days = excluded.revert_low_days,
    computed_at = now();

\else
\echo 'MONITOR_SKIPPED_INCOMPLETE 最新交易日只到了一个交易所，本轮不动快照'
\echo '  (抢早轮 16:00 常态：大商所先到、郑商所与上期所约 16:26。'
\echo '   保留上一版快照，等 17:30 那轮数据齐了再重算——不能先删后写残缺数据)'
\endif


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
       count(*) filter (where revert_low_n is not null
                           or revert_high_n is not null) 有回归率,
       -- 拐头素材的覆盖率:全空说明滚动窗那步被改坏,「已拐头」会整片消失,
       -- 而页面上只是「没有拐头行」,看不出是坏了。
       count(*) filter (where pair_pos_hi20 is not null) 有滚动位
  from spread_monitor_daily
 group by 1 order by 1 desc limit 5;

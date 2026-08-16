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
-- 源的范围：交易所官方，**外加大商所的三禾**。
--
-- 大商所没有官方那条路（WAF 全局拦截，见 DEC-041），三禾是唯一有历史的源，而
-- 鸡蛋、生猪、焦煤三个品种全在那里。原来这里只认 `*_official`，理由是三禾的增减是
-- 「清零差分」，拿它反推等于把错误再乘一遍——那个顾虑是对的，但范围划大了：
-- 清零差分只出现在**它自己回写过的那些行**上，指纹是 `增减 = −前一行持仓`，精确可辨。
-- 2026-08-12 全库实测：排除带指纹的行之后，三禾在榜日的增减在 32.8 万个相邻交易日
-- 样本上 99.997% 复现恒等式，与官方源一个水准；而真正要用到的「掉榜段之后的回榜行」
-- 那一侧，10,722 个样本里**一个指纹都没有**（回写行紧贴掉榜段末尾，天然是相邻的），
-- 反推值全部非负。
--
-- 带指纹的行由 fix-sanhe-fabricated-changes.sql 在管线里先修成真实增减。这里仍然把
-- 指纹再挡一道：那个脚本要是哪天没跑成，这里不该跟着写出错的数。
--
-- 幂等：先删本次窗口内自己写过的行，再重算。
--
-- 实现用带索引的临时表，不用一条巨型 CTE。**窗口参数必须真的缩小扫描范围**：
-- 第一版把整张 seat_history 拉进 CTE 做窗口函数，再在最后一步按日期过滤输出，
-- 于是 window_days=7 的日更和 window_days=7000 的全量回填一样贵——2026-08-12
-- 实测全量跑到十一分钟还没完，而日更管线里这一步每天都要跑。现在按
-- window_days 加 40 天余量先把行数砍掉，日更只碰最近几十天的数据。
-- 余量给的是长假：找「前一个交易日」时春节能隔十来天，卡得太紧会把边界那几天算漏。

\set ON_ERROR_STOP on
\if :{?window_days}
\else
\set window_days 7
\endif

begin;

delete from seat_history
 where source = 'reboard_inferred'
   and trade_date >= current_date - :window_days;

-- 「那天他在不在榜上」的判据。
--
-- **必须看所有行，不能只看有增减的行。** fix-sanhe-fabricated-zeros.sql 修过的行
-- 增减是空的（清零差分与修正后的持仓自相矛盾，只能留空）。拿「有增减的行」当判据，
-- 那些行会被当成「不在榜上」，于是给一个**已经知道持仓的日子**再写一条反推行——
-- 同一天同一会员两个数，而且都言之凿凿。
create temp table presence as
select distinct workspace_id, exchange, instrument, contract, rank_type, member, trade_date
  from seat_history
 where not is_variety_total and contract is not null
   and source <> 'reboard_inferred'
   and trade_date >= current_date - (:window_days + 40);

create index presence_key on presence
  (workspace_id, exchange, instrument, contract, rank_type, member, trade_date);
analyze presence;

-- 合约的交易日历。必须用它找「前一个交易日」，不能用 trade_date - 1：中间隔着
-- 周末与长假，减一天会指到一个根本没有数据的日子。
create temp table calendar_prev as
select workspace_id, exchange, instrument, contract, trade_date,
       lag(trade_date) over (partition by workspace_id, exchange, instrument, contract
                             order by trade_date) prev_date
  from (select distinct workspace_id, exchange, instrument, contract, trade_date
          from presence) d;

create index calendar_prev_key on calendar_prev
  (workspace_id, exchange, instrument, contract, trade_date);
analyze calendar_prev;

-- 反推的算术依据：带增减的行。
create temp table basis as
select workspace_id, exchange, instrument, contract, rank_type, member,
       trade_date, quantity, change
  from (
    select workspace_id, exchange, instrument, contract, rank_type, member,
           trade_date, quantity, change,
           lag(quantity) over m prev_q
      from seat_history
     where not is_variety_total and contract is not null and change is not null
       and trade_date >= current_date - (:window_days + 40)
       and (source like '%\_official'
            -- 三禾只有大商所的数据（郑商所、上期所那五个品种一行都没有），
            -- 写死 exchange 是为了将来它真的多出别家数据时不会悄悄混进来。
            or (source = 'sanhe' and exchange = 'DCE'))
    window m as (partition by workspace_id, exchange, instrument, contract,
                              rank_type, member
                 order by trade_date)
  ) t
 -- 清零差分指纹：增减恰好等于上一行持仓的相反数。见开头的说明。
 -- 这是第二道闸——fix-sanhe-fabricated-changes.sql 应该已经把它们修好了，
 -- 但那个脚本要是哪天没跑成，这里不该跟着写出错的数。
 where prev_q is null or change is distinct from -prev_q;

create index basis_key on basis
  (workspace_id, exchange, instrument, contract, trade_date);
analyze basis;

insert into seat_history (
    id, workspace_id, exchange, instrument, contract, is_variety_total,
    variety_total_is_computed, trade_date, rank_type, rank, member, quantity, change, source,
    updated_at)
select gen_random_uuid(), b.workspace_id, b.exchange, b.instrument, b.contract,
       false, false, c.prev_date, b.rank_type,
       -- 名次留空：他那天本来就不在榜上，编一个名次是凭空捏造。
       null, b.member, b.quantity - b.change,
       -- 增减留空。要算它得知道再前一天的持仓，而那天多半也不在榜上；留空是
       -- 「不知道」，填 0 是「没变化」——三禾的 1691 配 −2038 就是不肯留空的结果。
       null, 'reboard_inferred', now()
  from basis b
  join calendar_prev c
    on c.workspace_id = b.workspace_id and c.exchange = b.exchange
   and c.instrument = b.instrument and c.contract = b.contract
   and c.trade_date = b.trade_date
 where c.prev_date is not null
   and c.prev_date >= current_date - :window_days
   -- 前一个交易日他不在榜上：那天才需要反推。
   --
   -- 这里是对**带索引的临时表**做反连接，不是第一版那种在无索引 CTE 上逐行探测。
   -- 那一版生产上跑一小时没出来，改成窗口函数才活过来；后来窗口函数版本又因为
   -- 不受窗口参数约束而在日更里过慢。有索引 + analyze 之后规划器走哈希反连接，
   -- 两个毛病都没有——别看到 not exists 就往回改。
   and not exists (
       select 1 from presence p
        where p.workspace_id = b.workspace_id and p.exchange = b.exchange
          and p.instrument = b.instrument and p.contract = b.contract
          and p.rank_type = b.rank_type and p.member = b.member
          and p.trade_date = c.prev_date
   )
   -- 负持仓说明这一组数据本身有问题，宁可不写。
   and b.quantity - b.change >= 0
on conflict (workspace_id, trade_date, exchange, instrument, contract,
             is_variety_total, rank_type, member, source) do update set
    -- loaded_at 保持首次入库不动、updated_at 刷新(2026-08-16,口径见
    -- load-seats-direct.sql)。反推行本就被到达时刻画像按 source 排除,
    -- 但全部写入路径统一一个口径,不留例外。
    quantity = excluded.quantity, updated_at = now();

commit;

-- 落地核对。反推行数为 0 而窗口里明明有数据，多半是日历那一段写错了。
select instrument 品种, count(*) 反推行数,
       min(trade_date) 起, max(trade_date) 止, min(quantity) 最小持仓
  from seat_history
 where source = 'reboard_inferred'
 group by 1 order by 1;

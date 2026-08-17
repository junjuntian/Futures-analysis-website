-- 套利监控:加「近 20 个交易日当年轨位置的最高/最低」两列,给「已拐头」状态当素材。
--
-- 来历(DEC-063):运营者要一个全品种通用的进场规则。全样本回放证明「逢 3% 报警
-- 就做回归」在 265 个报警段上持到底中位是负的;分层之后才成立——第一层用历年
-- 回归统计筛资格(留一法验证:筛后 +29%,被筛掉的 −26%),第二层等价差自极值
-- 回撤**该组合自身区间的 10%** 再进(无量纲,自动适配各品种)。
--
-- 「已拐头」在读时的判定只需要两个事实:
--   · 近 20 个交易日内,当年轨位置曾进过报警带(位置的滚动最高 ≥ 0.97 即高位带,
--     滚动最低 ≤ 0.03 即低位带);
--   · 当前位置已退到带外足够远(高位 ≤ 0.90 / 低位 ≥ 0.10)。
--
-- 第二条用现有的 pair_position 就能判——**回撤区间的 10% 恰好等于位置退 10 个
-- 百分点**:报警时价差贴着滚动极值,极值就是区间端点,(端点−当前)/区间宽 =
-- 1 − 位置。所以唯一缺的素材是第一条的「近 20 日位置极值」,存成两列:
--
--   pair_pos_hi20 / pair_pos_lo20:当年轨位置在近 20 个交易日(含当日)的 max/min。
--
-- 20 个交易日是事实窗口不是阈值:回放里确认信号都在报警后 1~14 个交易日内出现,
-- 20 天覆盖住并自带过期——报警过去超过 20 天,hi20 滑出带外,拐头状态自动消失。
-- 报警带(0.97/0.03)与回撤量(10 个百分点)是阈值,留在 API 常量里,不落库。
--
-- 只算当年轨:资格统计(revert_*)与回放验证全部基于当年轨的可交易窗口,拐头
-- 跟着同一条轨,口径才闭环;历年轨(百分位区间)的位置可越界,「回撤=位置退
-- 10 个百分点」的等式在它身上不成立。
--
-- 幂等:同 202608170001/0003 的教训,必须能在已处于目标状态的库上重跑。

begin;

alter table spread_monitor_daily
    add column if not exists pair_pos_hi20 numeric,
    add column if not exists pair_pos_lo20 numeric;

-- 当年轨位置按构造落在 [0,1](区间端点就是含当日的滚动极值),它的滚动 max/min
-- 也必然在 [0,1] 且 hi >= lo、两列同生同灭。写成约束,算错了当场报出来。
alter table spread_monitor_daily
    drop constraint if exists spread_monitor_daily_pos20_sane,
    add constraint spread_monitor_daily_pos20_sane
        check ((pair_pos_hi20 is null) = (pair_pos_lo20 is null)
           and (pair_pos_hi20 is null
                or (pair_pos_hi20 between 0 and 1
                    and pair_pos_lo20 between 0 and 1
                    and pair_pos_hi20 >= pair_pos_lo20)));

insert into schema_versions (version, description)
values ('202608170004',
        'Spread monitor: 20-day rolling extremes of the pair-track position, the stored fact behind the read-time turned state')
on conflict (version) do nothing;

commit;

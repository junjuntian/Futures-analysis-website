-- 套利监控的回归率换口径:从「固定 20 个交易日看终点」改成「按可交易窗口的日历
-- 位置对齐、曾经触及即算回归」。运营者 2026-08-17 指出上一版(202608170001)算错。
--
-- 上一版错在哪
--
--   1. **固定 20 个交易日与合约剩余寿命无关。** 鸡蛋 09 合约 8 月末就退出散户可
--      交易区间,8 月中旬触发时只剩十来个交易日,拿 20 日后的价格去比,比的是已经
--      不可交易的东西。
--   2. **只看第 20 天那一个点。** 中途回落过、后来又涨回去的,按终点算是「没回归」,
--      可套利仓在那期间随时能平掉。
--
--   两处合起来把鸡蛋 09-01 的回归率算成 45%;换成新口径后,同一个模板从 6-01 起算
--   是 12 年 12 次都曾跌破起点。差距不是调参能补的,是口径错了。
--
-- 新口径
--
--   · **可交易窗口**照 5A 窗口引擎(rust/crates/domain/src/spread_analytics.rs),
--     不另立一套:起点 = 两腿都有数据的第一天(晚上市那条腿的上市日);止点 =
--     **先到期**那条腿的散户最后交易日,即 `last_weekday_before_delivery` ——
--     交割月前月的最后一个非周末日。那边注释写明与真实交易日历只差在节假日,
--     而节假日没有价格点,窗口裁剪结果等价。
--   · **历年按月-日对齐**:今天 6-01,就取历年各自的 6-01(非交易日顺延到之后第一个
--     交易日)当起点,一直看到各自窗口的止点。窗口可能跨年,所以锚点年份取「让这个
--     月-日落进该实例窗口」的那一个。
--   · **曾经触及**即算回归:高位看这段区间里最低收盘价差有没有跌破起点,低位看最高
--     有没有涨过起点。
--   · 只用**已走完**的年份实例(窗口止点晚于最新数据日的一律排除,含当年)——它们的
--     「后续」还没发生。
--   · 不要求历年该时点也处于极值(运营者定):这是季节性统计,绑上报警会把样本砍到
--     三四年。
--
-- 存三组数字而不是一个比率,因为**单看回归率会骗人**:
--
--   hit/n    「曾经触及」的年数。剩余期一长就趋近 100%(任何波动序列在足够长的窗口
--            里几乎必然回落一次)——苹果那些剩 128 个交易日的组合全是 100%,鸡蛋剩
--            12 天的才拉开到 75~92%。它只是个下限。
--   move     最有利那一刻相对起点走了多少点(择时平仓的上限)。
--   drift    **一直持到窗口止点**的净变化,已按方向标准化:正数 = 朝回归走。
--            JD2612/JD2701 是活证据:回归率 100%(12 年全都曾跌破起点),而持到到期
--            的中位是 −166 点,方向反的。只显示回归率会把这种组合读成安全机会。
--   days     历年剩余交易日中位数,给上面三个数一个时间尺度。
--
-- 幅度一律用**点数**(运营者定):价差会跨零,2019-08-14 起点 −8 点、回落 407 点,
-- 百分比算出来 5000%,毫无意义。
--
-- 不设样本年数门槛(运营者定):生猪只有 5 年、纯碱 6 年,设门槛整个品种就没统计了。
-- 年数原样存着,由界面写出来让人自己判断。
--
-- 202608170001 那 12 列(三档 × 低高)直接删:新口径**与阈值无关**(只跟日历位置和
-- 方向有关),分档没有意义;那批列上线仅一天,除本页外无消费方。

begin;

alter table spread_monitor_daily
    drop constraint if exists spread_monitor_daily_revert_low_sane,
    drop constraint if exists spread_monitor_daily_revert_high_sane,
    drop column if exists revert_low_hit_3,
    drop column if exists revert_low_n_3,
    drop column if exists revert_low_hit_5,
    drop column if exists revert_low_n_5,
    drop column if exists revert_low_hit_10,
    drop column if exists revert_low_n_10,
    drop column if exists revert_high_hit_3,
    drop column if exists revert_high_n_3,
    drop column if exists revert_high_hit_5,
    drop column if exists revert_high_n_5,
    drop column if exists revert_high_hit_10,
    drop column if exists revert_high_n_10;

alter table spread_monitor_daily
    -- 高位段:价差贴在区间上端,回归方向是走低。
    add column if not exists revert_high_hit integer,
    add column if not exists revert_high_n integer,
    add column if not exists revert_high_move numeric,
    add column if not exists revert_high_drift numeric,
    add column if not exists revert_high_days integer,
    -- 低位段:价差贴在区间下端,回归方向是走高。
    add column if not exists revert_low_hit integer,
    add column if not exists revert_low_n integer,
    add column if not exists revert_low_move numeric,
    add column if not exists revert_low_drift numeric,
    add column if not exists revert_low_days integer;

-- 命中数与样本数必须成对且 0 <= 命中 <= 样本、样本 > 0。
-- 样本为 0 存 NULL 而不是 0:`0/0` 在界面上会变成「0% 回归率」,那是最坏的一种错
-- ——看着像结论,其实是没有数据。
-- move 按定义非负(它是「最有利那一刻」到起点的距离);drift 可正可负,不设符号约束。
alter table spread_monitor_daily
    add constraint spread_monitor_daily_revert_pairs_sane
        check ((revert_high_hit is null) = (revert_high_n is null)
           and (revert_high_n is null
                or (revert_high_n > 0 and revert_high_hit between 0 and revert_high_n))
           and (revert_low_hit is null) = (revert_low_n is null)
           and (revert_low_n is null
                or (revert_low_n > 0 and revert_low_hit between 0 and revert_low_n))
           and (revert_high_move is null or revert_high_move >= 0)
           and (revert_low_move is null or revert_low_move >= 0)
           and (revert_high_days is null or revert_high_days > 0)
           and (revert_low_days is null or revert_low_days > 0));

insert into schema_versions (version, description)
values ('202608170002',
        'Spread monitor revert stats recomputed over the tradable window: calendar-aligned across years, touched-at-any-point, with move/drift/days alongside the hit rate')
on conflict (version) do nothing;

commit;

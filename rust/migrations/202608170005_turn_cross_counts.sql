-- 套利监控:加「近 20 个交易日拐头穿线次数」两列,给「信号差」降级标当素材。
--
-- 来历(DEC-063 修订):JM2609/JM2701 在 8 天里三次亮 ⚡(08-04/08-06/08-13),
-- 期间 08-11 还打回区间顶——按「创报警后新高离场」前两次进场都得止损。运营者
-- 拍板:同一组合短期内第二、三次穿线,读成「拐头质量差」的降级信号,页面要标出来。
-- 对照组:FG2701/SA2701 干脆的拐头(08-05 一次穿线一路走)不会累积次数。
--
--   turn_crosses_high_20 / turn_crosses_low_20:
--     近 20 个交易日(含当日)内,「拐头穿线」发生的次数。
--     高位穿线 = 前一日位置 > 0.90、当日 ≤ 0.90、且当日 hi20 ≥ 0.97(带内报过警);
--     低位对称(< 0.10 / ≥ 0.10 / lo20 ≤ 0.03)。
--
-- **例外声明:这两列烙进了 0.90/0.10/0.97 三个常量**,与「存位置不存结论」的
-- 惯例(202608120001)相悖。理由:次数是路径的函数,读时从单行推不出来;而这三个
-- 常量在 DEC-063 里被有意写死不做旋钮(与 API 的 TURN_BAND/TURN_RETREAT 同值,
-- 改一处必须同批改另一处)。若日后真要改常量,重跑 45 天窗口即可回填展示范围。
--
-- 幂等:同 0001/0003/0004 的教训。

begin;

alter table spread_monitor_daily
    add column if not exists turn_crosses_high_20 integer,
    add column if not exists turn_crosses_low_20 integer;

-- 20 行窗口里 0/1 求和,值域天然 [0,20];负数或超界说明求和层被改坏。
alter table spread_monitor_daily
    drop constraint if exists spread_monitor_daily_turn_crosses_sane,
    add constraint spread_monitor_daily_turn_crosses_sane
        check ((turn_crosses_high_20 is null
                or turn_crosses_high_20 between 0 and 20)
           and (turn_crosses_low_20 is null
                or turn_crosses_low_20 between 0 and 20));

insert into schema_versions (version, description)
values ('202608170005',
        'Spread monitor: rolling 20-day counts of turn-line crossings, the stored fact behind the poor-signal downgrade marker')
on conflict (version) do nothing;

commit;

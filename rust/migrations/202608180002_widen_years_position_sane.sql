-- 历年轨位置的 sane 护栏放宽到 ±100(DEC-071 历年实例回填时撞出)。
--
-- 当年轨位置是 running min/max 下的 (价差-低)/(高-低),恒在 [0,1],±10 的护栏
-- 绰绰有余,**保持不动**。历年轨不同:区间是同月模板跨年百分位 2.5~97.5,去掉
-- 极端值后区间被收窄,而当前价差可以远在区间之外——AP2111-AP2112 在 2021-11-04
-- 实测 prev_years_position = -12.26(历史低点 -8020 一撑,这是真实事实不是算错),
-- 首轮 5000 天回填被 [-10, 11] 拦死(insert 原子回滚,现网无损)。
--
-- 护栏的本意是捉「单位算错」级别的疯值(比如把百分比存成了点数),±100 仍然
-- 捉得住;历年位置十几倍区间宽度在极端年份是合法观测。
begin;

alter table spread_monitor_daily
    drop constraint if exists spread_monitor_daily_years_position_sane;
alter table spread_monitor_daily
    add constraint spread_monitor_daily_years_position_sane
    check (years_position is null
           or (years_position >= -100 and years_position <= 101));

alter table spread_monitor_daily
    drop constraint if exists spread_monitor_daily_prev_position_sane;
alter table spread_monitor_daily
    add constraint spread_monitor_daily_prev_position_sane
    check ((prev_pair_position is null
            or (prev_pair_position >= -10 and prev_pair_position <= 11))
       and (prev_years_position is null
            or (prev_years_position >= -100 and prev_years_position <= 101)));

insert into schema_versions (version, description)
values ('202608180002',
        'Widen the years-track position guards to +/-100: percentile ranges leave real observations far outside on extreme years')
on conflict (version) do nothing;

commit;

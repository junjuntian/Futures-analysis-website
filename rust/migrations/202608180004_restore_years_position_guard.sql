-- 把历年轨位置的 sane 护栏收回 ±10(撤销 202608180002 的放宽)。
--
-- 事情的完整经过(DEC-073):202608180002 放宽护栏的理由是「历史低点 −8020 是
-- 真实观测,百分位区间被撑爆」——**那个判断是错的**。−8020 来自 AP2111 在
-- 2021-11-03 的收盘价 0(当天无成交,郑商所如实写 0),前后两天的真实价差是
-- −363 与 −102。护栏当时正确拦住了脏数据,是我把守卫放宽了。
--
-- 根因已修(取价改 `coalesce(nullif(close_price,0), settlement_price)`),
-- 全量重算后 `years_position` 的实际值域是 **−2.85 ~ 5.01**,原来的 ±10
-- 有充足余量。守卫收回去:护栏越紧,下一次脏数据越早被拦下——这次就是它先
-- 报的警。
--
-- 先重算再收紧,顺序不能反:表里还留着旧值时收紧会被 check 当场拒绝。

begin;

alter table spread_monitor_daily
    drop constraint if exists spread_monitor_daily_years_position_sane;
alter table spread_monitor_daily
    add constraint spread_monitor_daily_years_position_sane
    check (years_position is null
           or (years_position >= -10 and years_position <= 11));

alter table spread_monitor_daily
    drop constraint if exists spread_monitor_daily_prev_position_sane;
alter table spread_monitor_daily
    add constraint spread_monitor_daily_prev_position_sane
    check ((prev_pair_position is null
            or (prev_pair_position >= -10 and prev_pair_position <= 11))
       and (prev_years_position is null
            or (prev_years_position >= -10 and prev_years_position <= 11)));

insert into schema_versions (version, description)
values ('202608180004',
        'Restore the +/-10 guard on years positions: the value that forced it open was dirty data, not a real observation')
on conflict (version) do nothing;

commit;

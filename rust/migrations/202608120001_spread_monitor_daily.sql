-- 套利监控的每日快照。
--
-- 为什么要落表而不是每次现算：完整口径（91 组组合 × 两条历史轨）在生产上实测
-- 1224 毫秒（futures_runtime + RLS 下量的），开一次页面等一秒二不能接受。而运营者
-- 要的「触发留记录」本来就得逐日存，一张表两件事一起办。
--
-- **存的是位置，不是触发结论。** 阈值（落在两端多少算触发）留到读的时候再套：
-- 存了结论就等于把阈值焊死，日后想把 10% 调成 15% 得重算全部历史；存位置则任何
-- 阈值都能在任何一天上重新判定，历史也跟着一起变，不会出现新旧阈值混在一张表里
-- 的局面。
--
-- 两条轨的口径不同，这是有意的：
--   当年（pair_*）  该合约对自身从上市到今天的原始最低/最高。序列短，去极端值
--                   会把本来就没几个点的区间削没，所以不去。
--   历年（years_*） 同月份组合在所有年份上的第 2.5 / 97.5 百分位。苹果历年最低是
--                   −10686（2018 年那波留下的），不去极端值会把区间撑到 12536，
--                   让百分比长期贴在中间不动——去掉两端各 2.5% 之后是 −848 ~ 631。

begin;

create table spread_monitor_daily (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    trade_date date not null,

    -- 先到期的那条腿在前，与套利页的腿序规则一致。
    instrument_1 text not null,
    contract_1 text not null,
    instrument_2 text not null,
    contract_2 text not null,
    -- 玻璃−纯碱这类跨品种同月组合。界面要把它和跨月组合区分开。
    is_cross_variety boolean not null,

    spread numeric not null,

    pair_days integer not null,
    pair_low numeric not null,
    pair_high numeric not null,
    -- 区间退化（最高等于最低）时位置无意义，留空而不是填 0——填 0 会被读成「在最低点」。
    pair_position numeric,

    years_days integer,
    years_low numeric,
    years_high numeric,
    years_position numeric,

    computed_at timestamptz not null default now(),

    constraint spread_monitor_daily_identity
        unique (workspace_id, trade_date, contract_1, contract_2),
    constraint spread_monitor_daily_legs_differ check (contract_1 <> contract_2),
    constraint spread_monitor_daily_instrument_shape
        check (instrument_1 ~ '^[A-Z]{1,2}$' and instrument_2 ~ '^[A-Z]{1,2}$'),
    -- 区间的上下界不许颠倒。写反了图会画成镜像，而镜像的图看着完全正常。
    constraint spread_monitor_daily_pair_range check (pair_high >= pair_low),
    constraint spread_monitor_daily_years_range check (years_high >= years_low),
    -- 位置在 [0,1] 之外是允许的：历年轨用的是百分位区间，当前价差可以落在
    -- 第 2.5 百分位之下或第 97.5 之上。但两端各留一个数量级的余量当护栏，
    -- 算错了（比如除数取反）会当场报出来而不是画一条离谱的线。
    constraint spread_monitor_daily_position_sane
        check (pair_position is null or pair_position between -10 and 11),
    constraint spread_monitor_daily_years_position_sane
        check (years_position is null or years_position between -10 and 11)
);

create index spread_monitor_daily_by_date
    on spread_monitor_daily (workspace_id, trade_date desc);

alter table spread_monitor_daily enable row level security;
alter table spread_monitor_daily force row level security;

create policy spread_monitor_daily_workspace on spread_monitor_daily
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

-- 只读：算这张表的是日更管线（futures_app），API 侧只查。
grant select on spread_monitor_daily to futures_runtime;

insert into schema_versions (version, description)
values ('202608120001', 'Daily spread monitor snapshot storing range positions, not verdicts')
on conflict (version) do nothing;

commit;

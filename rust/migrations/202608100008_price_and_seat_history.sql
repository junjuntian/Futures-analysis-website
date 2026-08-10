begin;

-- 品种历史价格 与 席位持仓：运营者 2026-08-10 定的两张表。
--
-- 设计写在 docs/TWO_TABLE_DESIGN.md，依据是 docs/RAW_FIELD_INVENTORY.md 里
-- 实际取到的文件。定这两张表的唯一目标是运营者那句话——**数据库要很容易看**——
-- 所以交易所、品种、合约都是可读文本而不是外键，一行就是一个事实，
-- 不必先读代码或连三张表才知道自己在看什么。
--
-- 与既有 market_prices / seat_positions 的关系：那两张走的是审计导入通道，
-- 由每日自动采集写入，暂时保留不动。这两张是历史回填与两个产品（套利、席位）
-- 直接读的表。合并是后续的事，在此之前两边并存这一点必须是知情的，不是忘了。

create table price_history (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,

    exchange text not null,
    instrument text not null,
    -- 统一大写 + 四位月份：AP2501、JM1601、AU2412。
    -- 郑商所原文是三位月份的 AP501，世纪按该品种上市年份推出来。
    contract text not null,
    trade_date date not null,

    open_price numeric(20, 8),
    high_price numeric(20, 8),
    low_price numeric(20, 8),
    close_price numeric(20, 8),
    settlement_price numeric(20, 8),
    prev_settlement_price numeric(20, 8),

    -- 手。不做单双边换算：大商所双边、郑商所 2020 起单边，除以二得到的是
    -- 我们编的数而不是交易所公布的数。口径记在下一列，由用数的人决定怎么用。
    volume numeric(24, 8),
    volume_basis text not null,
    -- 元。郑商所与上期所原文是万元，入库时已 ×10000。
    -- 不统一的话同一列里两种量纲，跨所比较和
    -- 「成交额 ÷（成交量 × 结算价）= 点值」这条校验都会失效。
    turnover numeric(28, 8),

    open_interest numeric(24, 8),
    open_interest_change numeric(24, 8),

    source text not null,
    loaded_at timestamptz not null default now(),

    constraint price_history_identity unique (workspace_id, contract, trade_date, source),
    constraint price_history_exchange_allowed check (exchange in ('DCE', 'CZCE', 'SHFE')),
    constraint price_history_contract_shape check (contract ~ '^[A-Z]{1,2}[0-9]{4}$'),
    constraint price_history_instrument_shape check (instrument ~ '^[A-Z]{1,2}$'),
    constraint price_history_volume_basis_allowed check (volume_basis in ('single', 'double')),
    -- 至少要有一个价格，否则这一行没有在陈述任何事情。
    constraint price_history_has_a_price check (
        close_price is not null or settlement_price is not null
    ),
    -- 有区间就必须自洽。当日无成交时四项皆空（不写 0），所以这里允许全空。
    constraint price_history_range_ordered check (
        high_price is null or low_price is null or high_price >= low_price
    ),
    constraint price_history_not_negative check (
        coalesce(volume, 0) >= 0 and coalesce(turnover, 0) >= 0
        and coalesce(open_interest, 0) >= 0
    )
);

create index price_history_by_contract on price_history (workspace_id, contract, trade_date);
create index price_history_by_instrument on price_history (workspace_id, instrument, trade_date);

create table seat_history (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,

    exchange text not null,
    instrument text not null,
    -- 品种汇总行没有合约。
    contract text,
    is_variety_total boolean not null,
    -- 官方发的汇总（只有郑商所）与我们自己加总的，必须分得出来：
    -- 各合约前 20 的并集不等于品种前 20，两者未必相等。
    variety_total_is_computed boolean not null default false,
    trade_date date not null,

    rank_type text not null,
    -- 交易所只公布前 20，这是上限不是我们的截断。
    -- 三禾不给名次，那三个大商所品种此列为空——如实留空，不编。
    rank integer,
    member text not null,
    quantity numeric(24, 8) not null,
    change numeric(24, 8),

    source text not null,
    loaded_at timestamptz not null default now(),

    constraint seat_history_identity unique (
        workspace_id, trade_date, exchange, instrument, contract,
        is_variety_total, rank_type, member, source
    ),
    constraint seat_history_exchange_allowed check (exchange in ('DCE', 'CZCE', 'SHFE')),
    constraint seat_history_instrument_shape check (instrument ~ '^[A-Z]{1,2}$'),
    constraint seat_history_rank_type_allowed check (rank_type in ('volume', 'long', 'short')),
    constraint seat_history_rank_positive check (rank is null or rank > 0),
    constraint seat_history_member_not_blank check (length(trim(member)) > 0),
    constraint seat_history_quantity_not_negative check (quantity >= 0),
    -- 合约与「是否品种汇总」必须一致：汇总行没有合约，逐合约行必须有。
    constraint seat_history_contract_matches_total check (
        (is_variety_total and contract is null)
        or (not is_variety_total and contract ~ '^[A-Z]{1,2}[0-9]{4}$')
    ),
    -- 只有汇总行才谈得上是不是自算的。
    constraint seat_history_computed_only_for_totals check (
        is_variety_total or not variety_total_is_computed
    )
);

create index seat_history_by_contract on seat_history (workspace_id, contract, trade_date);
create index seat_history_by_member on seat_history (workspace_id, member, instrument, trade_date);
create index seat_history_by_instrument on seat_history (workspace_id, instrument, trade_date);

alter table price_history enable row level security;
alter table price_history force row level security;
alter table seat_history enable row level security;
alter table seat_history force row level security;

create policy price_history_workspace_isolation on price_history
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());
create policy seat_history_workspace_isolation on seat_history
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

-- 读由 API 承担，写由回填程序承担。回填以 futures_app 连库（超级用户，
-- 绕过 RLS），所以这里只授运行角色读，避免它意外改历史。
grant select on price_history, seat_history to futures_runtime;

insert into schema_versions (version, description)
values ('202608100008', 'Flat price and seat history tables for the two products')
on conflict (version) do nothing;

commit;

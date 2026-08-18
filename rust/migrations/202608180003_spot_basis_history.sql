-- 现货价与基差(DEC-074)。数据源:生意社(经 akshare `futures_spot_price`)。
--
-- 为什么要它:平台此前只有期货价,产业侧的「现货多少钱」完全缺失,而《体系》
-- 模块一把「期现基差」列为必看项。基差 = 现货 − 期货:为正是期货贴水,为负是
-- 期货升水。跨期套利的两条腿相对同一个现货,所以**两个基差之差就是跨期价差
-- 本身,信息重复**——真正的增量在现货价序列与基差的绝对水平/历史分位:
-- 它回答的是「现在期货整体比现货贵/便宜到什么程度」,这是期货价格里没有的
-- 独立信息,先存事实,判定放到读时。
--
-- 探针结论(2026-08-18):JD/JM/LH/FG/SA 五个监控品种有数据,**苹果 AP 没有**
-- (生意社无苹果现货报价);历史 FG/JD/JM 到 2018、SA 到 2020、LH 到 2022
-- (上市即有);与库内期货收盘价逐笔对拍 26/27 完全一致(第 27 笔恰好挖出
-- 我方的收盘价 0 缺陷,见 DEC-073)。
--
-- 一天一品种一行。近月与主力两套合约/价格/基差**如实照存源的口径**:源自己
-- 挑近月与主力,我们不重挑——重挑就得复制它的规则,两套规则迟早分叉。

begin;

create table if not exists spot_basis_history (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null references workspaces(id) on delete restrict,
    trade_date date not null,
    instrument text not null,

    spot_price numeric(20, 8) not null,
    -- 近月腿:合约代码照源写(郑商所在源里是三位年月,如 FG609),入库前已归一
    -- 成四位(FG2609),与 price_history 的 contract 可直接对上。
    near_contract text,
    near_price numeric(20, 8),
    near_basis numeric(20, 8),
    near_basis_rate numeric(20, 8),
    -- 主力腿。
    dominant_contract text,
    dominant_price numeric(20, 8),
    dominant_basis numeric(20, 8),
    dominant_basis_rate numeric(20, 8),

    source text not null,
    loaded_at timestamptz not null default now(),

    constraint spot_basis_history_identity
        unique (workspace_id, trade_date, instrument, source),
    constraint spot_basis_history_instrument_shape check (instrument ~ '^[A-Z]{1,2}$'),
    -- 现货价必须是价格。收盘价 0 的教训(DEC-073)同样适用于现货:
    -- 源偶尔会把「没有报价」写成 0,那不是价格,不许入库。
    constraint spot_basis_history_spot_positive check (spot_price > 0),
    constraint spot_basis_history_near_positive
        check (near_price is null or near_price > 0),
    constraint spot_basis_history_dominant_positive
        check (dominant_price is null or dominant_price > 0)
);

create index if not exists spot_basis_history_lookup
    on spot_basis_history (workspace_id, instrument, trade_date desc);

alter table spot_basis_history enable row level security;
alter table spot_basis_history force row level security;

drop policy if exists spot_basis_history_workspace on spot_basis_history;
create policy spot_basis_history_workspace on spot_basis_history
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

grant select, insert, update, delete on spot_basis_history to futures_runtime;

insert into schema_versions (version, description)
values ('202608180003',
        'Spot prices and basis from Shengyishe via akshare: the industry side the platform never had')
on conflict (version) do nothing;

commit;

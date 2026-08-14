-- 总览页「黄金白银报告表」的两块可编辑内容。
--
-- 这张表上下两半的来源完全不同，所以分两张表存，不要合成一张：
--   上半（压力位/支撑位）—— **运营者手工填**，是他自己的盘面判断，平台无从计算。
--   下半（席位净持仓与筹码）—— **全自动**，从 seat_history/price_history 现算，不落表。
--
-- 下半不落表的理由：净持仓是一次聚合、筹码走已有的成本引擎（`build_variety_series`），
-- 且只需要当日持仓合约的那几段历史，实测够快。落表就要多一条日更管线和一份可能与
-- 席位页对不上的副本——本项目已经在「同一概念两处实现」上栽过两次。

begin;

-- ---------------------------------------------------------------------------
-- 上半：压力位/支撑位网格
-- ---------------------------------------------------------------------------
--
-- **按交易日存，不是存一份「当前值」。** 报告是按日的（标题就写着日期），只存
-- 一份当前值意味着翻回上周的报告会看到今天的压力位，而看上去毫无异常。按日存还
-- 顺带留下了判断的轨迹。界面在某天尚无记录时用「最近一个有记录的日子」预填，
-- 运营者只改动了的格子，不必每天重敲一遍。
--
-- 网格本身用 jsonb：行列结构是运营者的方法论（七行、三列行情 + 偏向 + 关注度），
-- 不是我们的领域模型，焊进 DDL 只会让他每次微调都要一次迁移。形状由 Rust 侧
-- 校验，这里只守住「必须是对象且带 rows 数组」这条底线。
create table overview_report_levels (
    workspace_id uuid not null references workspaces(id) on delete restrict,
    trade_date date not null,
    cells jsonb not null,
    updated_at timestamptz not null default now(),

    primary key (workspace_id, trade_date),
    constraint overview_report_levels_shape
        check (jsonb_typeof(cells) = 'object' and jsonb_typeof(cells -> 'rows') = 'array')
);

alter table overview_report_levels enable row level security;
alter table overview_report_levels force row level security;

create policy overview_report_levels_workspace on overview_report_levels
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

-- ---------------------------------------------------------------------------
-- 下半：席位分组
-- ---------------------------------------------------------------------------
--
-- 四组，各自的含义不同，别合并：
--   institution 机构席位 —— 逐行显示，另出一行「机构持仓」合计
--   watch       其他关注 —— 逐行显示，**不进任何合计**（中财就属于这一档）
--   foreign     外资席位 —— 逐行显示，另出一行「外资持仓」合计
--   retail      散户席位 —— **只出合计行**，不逐行显示
--
-- 写成配置而不是写死名单：运营者调整过一次信号组（`DEC-051` 移除国投），
-- 再调不该需要发一次版。
create table overview_report_seat_groups (
    workspace_id uuid not null references workspaces(id) on delete restrict,
    group_key text not null,
    -- 会员名按**归一后**的写法存（去掉尾部括号、套用更名别名），与席位页同一口径；
    -- 查询时再展开成该名字的全部历史写法。
    members text[] not null,
    updated_at timestamptz not null default now(),

    primary key (workspace_id, group_key),
    constraint overview_report_seat_groups_known
        check (group_key in ('institution', 'watch', 'foreign', 'retail')),
    -- 空数组合法（某一组不想要就清空），但不许有空字符串成员——那会匹配不到任何
    -- 席位却在表上占一行，看起来像「这家今天没持仓」。
    constraint overview_report_seat_groups_no_blank
        check (not (members && array['']))
);

alter table overview_report_seat_groups enable row level security;
alter table overview_report_seat_groups force row level security;

create policy overview_report_seat_groups_workspace on overview_report_seat_groups
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

-- 这两张表由界面直接读写（运营者手填），所以 runtime 要有写权限——
-- 与那些「日更算、API 只读」的表不同。
grant select, insert, update, delete on overview_report_levels to futures_runtime;
grant select, insert, update, delete on overview_report_seat_groups to futures_runtime;

insert into schema_versions (version, description)
values ('202608150001', 'Overview gold/silver report: operator-edited price levels and configurable seat groups')
on conflict (version) do nothing;

commit;

-- 席位组合收藏：净持仓页一次要选好几家会员，每次重选一遍是纯粹的重复劳动。
--
-- 为什么进库而不是存浏览器：这是运营者的一份「关注名单」，是他的判断沉淀，
-- 不该因为清一次缓存或换台电脑就没了。价差分析的收藏（spread_favorites）
-- 已经是这个做法，此处照搬。
--
-- 不记 created_by：这台面板的活人使用者就是运营者本人，多一个外键只是多一处
-- 可能出错的地方。overview_report 那两张表也是这么定的。

begin;

create table seat_member_favorites (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    name text not null,
    -- 会员名按**归一后**的写法存（去掉尾部括号、套用更名别名），与席位页同一口径；
    -- 查询时再展开成该名字的全部历史写法。
    members text[] not null,
    created_at timestamptz not null default now(),

    constraint seat_member_favorites_workspace_identity unique (workspace_id, id),
    -- 收藏栏上只显示名字，同名两组会员是分不清的。
    constraint seat_member_favorites_name_unique unique (workspace_id, name),
    constraint seat_member_favorites_name_not_blank
        check (length(trim(name)) between 1 and 40),
    -- 上限十家，与界面一致。
    --
    -- 下限必须写成 coalesce：array_length 对空数组返回 null，而 CHECK 在结果为
    -- null 时是**放行**的——不裹一层的话空收藏会被存进来，点一下什么都没选中，
    -- 看上去像收藏坏了。
    constraint seat_member_favorites_size
        check (coalesce(array_length(members, 1), 0) between 1 and 10),
    constraint seat_member_favorites_no_blank
        check (not (members && array[''])),
    -- 成员去重挡在 Rust 侧：CHECK 里不允许子查询，这里写不出「元素互不相同」。
    -- 同一家进来两次会被算两遍，合计直接翻倍——那是这张表最该防的事，
    -- 挡在应用层不等于可以不挡。
    constraint seat_member_favorites_created_at_sane
        check (created_at > timestamptz '2020-01-01')
);

create index seat_member_favorites_workspace_created_idx
    on seat_member_favorites (workspace_id, created_at desc);

alter table seat_member_favorites enable row level security;
alter table seat_member_favorites force row level security;

create policy seat_member_favorites_workspace on seat_member_favorites
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

-- 由界面直接读写（运营者自己收藏、自己删），runtime 要有写权限。
grant select, insert, delete on seat_member_favorites to futures_runtime;

insert into schema_versions (version, description)
values ('202608150002', 'Seat member favorites: named seat combinations for the net-position page')
on conflict (version) do nothing;

commit;

-- 这两个产品覆盖哪八个品种，落进库里。
--
-- 这份清单此前只存在于回填脚本的一个集合字面量里（`backfill/parsers.py` 的 WANT）。
-- 日更投影一接上来就需要它：market_prices 里有五家交易所、六十来个品种，全量投影会让
-- 套利页的品种下拉冒出一堆只有三天历史的品种——点进去是空图，而空图比没有这个选项
-- 更糟，因为它看起来像是数据坏了。
--
-- 存成表而不是写死在 SQL 里：运营者以后要加第九个品种时，改一行数据并补一次回填即可，
-- 不必改代码再发一次版。

begin;

create table product_instrument_scope (
    workspace_id uuid not null references workspaces(id) on delete restrict,
    exchange text not null,
    instrument text not null,
    -- 界面上显示的品种名。不用 instruments.name：那张表是采集侧按上游给的名字填的，
    -- 眼下就不一致——焦煤是「焦煤」，玻璃是「平板玻璃期货」，黄金白银干脆存的是
    -- 代码 AU/AG。品种名是给人看的，得由这里定，不该随上游的措辞漂移。
    display_name text not null,
    added_at timestamptz not null default now(),

    constraint product_instrument_scope_identity primary key (workspace_id, instrument),
    constraint product_instrument_scope_exchange check (exchange in ('DCE', 'CZCE', 'SHFE')),
    constraint product_instrument_scope_shape check (instrument ~ '^[A-Z]{1,2}$'),
    constraint product_instrument_scope_name_not_blank check (length(trim(display_name)) > 0)
);

alter table product_instrument_scope enable row level security;
alter table product_instrument_scope force row level security;

create policy product_instrument_scope_workspace on product_instrument_scope
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

grant select on product_instrument_scope to futures_runtime;

do $$
declare
    target uuid;
    inserted integer;
begin
    -- 迁移角色既不是超级用户也没有 BYPASSRLS：不逐个 workspace 设置
    -- app.current_workspace_id，插入会被 with check 全部挡掉，而随后的计数又会被
    -- using 挡成空集，于是「0 == 0」假装通过。这个坑在 202608100006 踩过一次。
    for target in select id from workspaces loop
        perform set_config('app.current_workspace_id', target::text, true);
        if app.current_workspace_id() is distinct from target then
            raise exception 'workspace 上下文没设上，写进去的行会归错人';
        end if;

        insert into product_instrument_scope (workspace_id, exchange, instrument, display_name)
        values
            (target, 'DCE',  'JM', '焦煤'),
            (target, 'DCE',  'JD', '鸡蛋'),
            (target, 'DCE',  'LH', '生猪'),
            (target, 'CZCE', 'FG', '玻璃'),
            (target, 'CZCE', 'AP', '苹果'),
            (target, 'CZCE', 'SA', '纯碱'),
            (target, 'SHFE', 'AU', '黄金'),
            (target, 'SHFE', 'AG', '白银')
        on conflict (workspace_id, instrument) do nothing;

        select count(*) into inserted
          from product_instrument_scope
         where workspace_id = target;
        if inserted <> 8 then
            raise exception 'workspace % 的品种范围应当是 8 个，实际 %', target, inserted;
        end if;
    end loop;
end
$$;

insert into schema_versions (version, description)
values ('202608100011', 'The eight instruments the two products cover')
on conflict (version) do nothing;

commit;

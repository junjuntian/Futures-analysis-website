-- 三个新品种进产品范围(DEC-158,2026-08-30 运营者:铁矿石之外再加上证50与原油)。
--
-- product_instrument_scope 是行情装载(load-dce-daily.sql join 它过滤)与套利页
-- 品种下拉的**共同白名单**。铁矿石 DEC-156 加了席位白名单/行情 WANT/点值三处,
-- **漏了这第四处**——没有 scope 行,行情 CSV 采回来也进不了 price_history。
--
-- 首版在生产被 RLS 当场拒掉(2026-08-30 部署失败,整轮回滚):scope 表是
-- FORCE row level security,而**迁移角色没有 BYPASSRLS**——202608100011 的注释
-- 就写着这一点(202608100006 踩过的坑),我却照抄了 202608100003 里相反的说法。
-- 正确做法与建表迁移一字同:逐 workspace set_config 再插,插完当场断言。
begin;

alter table product_instrument_scope
    drop constraint product_instrument_scope_exchange;
alter table product_instrument_scope
    add constraint product_instrument_scope_exchange
        check (exchange in ('DCE', 'CZCE', 'SHFE', 'CFFEX', 'INE'));

do $$
declare
    target uuid;
    inserted integer;
begin
    for target in select id from workspaces loop
        perform set_config('app.current_workspace_id', target::text, true);
        if app.current_workspace_id() is distinct from target then
            raise exception 'workspace 上下文没设上，写进去的行会归错人';
        end if;

        insert into product_instrument_scope (workspace_id, exchange, instrument, display_name)
        values
            (target, 'DCE',   'I',  '铁矿石'),
            (target, 'CFFEX', 'IH', '上证50'),
            (target, 'INE',   'SC', '原油')
        on conflict (workspace_id, instrument) do nothing;

        select count(*) into inserted
          from product_instrument_scope
         where workspace_id = target
           and instrument in ('I', 'IH', 'SC');
        if inserted <> 3 then
            raise exception 'workspace % 应有 3 个新品种,实际 %', target, inserted;
        end if;

        -- 点值也在同一循环里做:IH 300 元/点,SC 1000 桶/手。只 update 不 insert
        -- (instruments 行由 catalog 采集建,带血缘外键);行还没出现时由
        -- load-catalog-direct.sql 末尾的兜底补(改点值三处同改,DEC-156)。
        update instruments set price_multiplier = spec.m, updated_at = now()
          from (values ('IH', 300::numeric), ('SC', 1000::numeric)) as spec(code, m)
         where instruments.workspace_id = target
           and upper(instruments.code) = spec.code
           and (instruments.price_multiplier is null or instruments.price_multiplier <> spec.m);
    end loop;
end
$$;

insert into schema_versions (version, description)
values ('202608300001',
        'Product scope + multipliers for iron ore (I), SSE50 futures (IH, CFFEX), crude oil (SC, INE)')
on conflict (version) do nothing;

commit;

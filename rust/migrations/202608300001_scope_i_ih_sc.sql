-- 三个新品种进产品范围(DEC-158,2026-08-30 运营者:铁矿石之外再加上证50与原油)。
--
-- product_instrument_scope 是行情装载(load-dce-daily.sql join 它过滤)与套利页
-- 品种下拉的**共同白名单**。铁矿石 DEC-156 加了席位白名单/行情 WANT/点值三处,
-- **漏了这第四处**——没有 scope 行,行情 CSV 采回来也进不了 price_history。
--
-- 交易所约束原来只放 DCE/CZCE/SHFE:IH 在中金所、SC 在能源中心,先放宽。
begin;

alter table product_instrument_scope
    drop constraint product_instrument_scope_exchange;
alter table product_instrument_scope
    add constraint product_instrument_scope_exchange
        check (exchange in ('DCE', 'CZCE', 'SHFE', 'CFFEX', 'INE'));

-- 逐 workspace 播(与 202608100011 同):scope 有 RLS,迁移角色 BYPASSRLS。
insert into product_instrument_scope (workspace_id, exchange, instrument, display_name)
select w.id, v.exchange, v.code, v.display_name
  from workspaces w
  cross join (values
    ('DCE',   'I',  '铁矿石'),
    ('CFFEX', 'IH', '上证50'),
    ('INE',   'SC', '原油')
  ) as v(exchange, code, display_name)
on conflict (workspace_id, instrument) do nothing;

-- 点值:IH 300 元/点(合约乘数),SC 1000 桶/手(0.1 元/桶最小变动 → 100 元/手)。
-- 只 update:instruments 行由 catalog 采集建(带血缘外键),凭空插行是错的;
-- 行还没出现时由 load-catalog-direct.sql 末尾的兜底补(改点值三处同改,DEC-156)。
update instruments set price_multiplier = spec.m, updated_at = now()
  from (values ('IH', 300::numeric), ('SC', 1000::numeric)) as spec(code, m)
 where upper(instruments.code) = spec.code
   and (instruments.price_multiplier is null or instruments.price_multiplier <> spec.m);

insert into schema_versions (version, description)
values ('202608300001',
        'Product scope + multipliers for iron ore (I), SSE50 futures (IH, CFFEX), crude oil (SC, INE)')
on conflict (version) do nothing;

commit;

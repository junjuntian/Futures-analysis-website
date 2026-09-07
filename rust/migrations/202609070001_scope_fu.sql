-- 燃料油 FU 进产品范围(2026-09-07 运营者:「把 fu 燃油这个品种的数据和席位导入
-- 我的网站,相当于加一个品种」)。
--
-- **上期所,不是能源中心**:低硫燃料油 LU 才是能源中心的。两者中文名只差两个字,
-- 交易所是 price_history / seat_history 身份键的一部分 —— 标错的话同一根合约会
-- 写出两份互不去重的行,页面上一个合约出现两次而两边数字都对
-- (`parsers.EXCHANGE_BY_VARIETY` 的注释里警告过的那种最难看出来的错)。
--
-- 点值 10 吨/手,**不是照规格书抄的,是从数据里算的**:2026-08-28 上期所 kx 文件里
-- fu 的七根合约,`成交额 ÷ (成交量 × 结算价)` 逐根都得 10.00。
--
-- scope 表是 FORCE row level security,而迁移角色**没有 BYPASSRLS**
-- (202608300001 首版在生产被当场拒掉、整轮回滚)。所以照它的正确写法:
-- 逐 workspace set_config 再插,插完当场断言。
begin;

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
        values (target, 'SHFE', 'FU', '燃料油')
        on conflict (workspace_id, instrument) do nothing;

        select count(*) into inserted
          from product_instrument_scope
         where workspace_id = target and instrument = 'FU';
        if inserted <> 1 then
            raise exception 'workspace % 的燃料油 scope 行没进去', target;
        end if;

        -- 点值。只 update 不 insert:instruments 行由 catalog 采集建(带血缘外键),
        -- 行还没出现时由 load-catalog-direct.sql 末尾的兜底补 —— 改点值三处同改
        -- (本迁移 / load-catalog-direct.sql / DEC-156 的清单)。
        update instruments set price_multiplier = spec.m, updated_at = now()
          from (values ('FU', 10::numeric)) as spec(code, m)
         where instruments.workspace_id = target
           and upper(instruments.code) = spec.code
           and (instruments.price_multiplier is null or instruments.price_multiplier <> spec.m);
    end loop;
end
$$;

insert into schema_versions (version, description)
values ('202609070001', 'Product scope + multiplier for fuel oil (FU, SHFE, 10 t/lot)')
on conflict (version) do nothing;

commit;

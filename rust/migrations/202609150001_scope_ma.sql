-- 甲醇 MA 进产品范围(2026-09-15 运营者:「网站加上甲醇这个品种进去」)。
--
-- 郑商所。点值 **10 吨/手** —— **不是照规格书抄的,是从数据里算的**:
-- 2026-09-14 郑商所行情里 MA610 / MA611 / MA612 / MA701 四根合约,
-- `成交额 ÷ (成交量 × 结算价)` 逐根都得 10.00。
--
-- **只收 MA,绝不收 ME。** 郑商所 2014 年中起把甲醇代码从 `ME` 换成 `MA`,
-- **同时把合约单位从 50 吨/手改成 10 吨/手**(2015-06 前后 ME 全部退完:
-- 2014-07-01 实测 MA 1 根 / ME 11 根,2015-04-01 是 MA 10 / ME 2,2015-06-01 起 ME 归零)。
-- 两个代码是两个不同的合约,点值差 5 倍 —— 混进一个品种,盈亏整段算错。
-- `parsers.WANT` 里没有 "ME",ME 的行会被自然滤掉;这段话是写给
-- 日后想「把历史补全」的人看的。
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
        values (target, 'CZCE', 'MA', '甲醇')
        on conflict (workspace_id, instrument) do nothing;

        select count(*) into inserted
          from product_instrument_scope
         where workspace_id = target and instrument = 'MA';
        if inserted <> 1 then
            raise exception 'workspace % 的甲醇 scope 行没进去', target;
        end if;

        -- 点值。只 update 不 insert:instruments 行由 catalog 采集建(带血缘外键),
        -- 行还没出现时由 load-catalog-direct.sql 末尾的兜底补 —— 改点值三处同改
        -- (本迁移 / load-catalog-direct.sql / 加品种清单)。
        update instruments set price_multiplier = spec.m, updated_at = now()
          from (values ('MA', 10::numeric)) as spec(code, m)
         where instruments.workspace_id = target
           and upper(instruments.code) = spec.code
           and (instruments.price_multiplier is null or instruments.price_multiplier <> spec.m);
    end loop;
end
$$;

insert into schema_versions (version, description)
values ('202609150001', 'Product scope + multiplier for methanol (MA, CZCE, 10 t/lot)')
on conflict (version) do nothing;

commit;

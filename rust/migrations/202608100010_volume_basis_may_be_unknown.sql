-- 成交量口径可以是「不知道」。
--
-- 回填时每个交易所的口径都是从文件里实测出来的，所以那批行都填得出来。日更走的是
-- market_prices，那张表没有记口径；能不能判定取决于当天有没有成交额和结算价
-- （靠「成交额 ÷（成交量 × 结算价）」比对点值来判定单边还是双边）。判定不了的时候，
-- 硬填一个 'single' 就是编数据——这张表的整个前提是一行只陈述一个事实。
--
-- 留空的含义是**口径未知**，不是单边。读这列的人必须把 null 当作「别拿这个量去跨所比」。

begin;

alter table price_history alter column volume_basis drop not null;

do $$
begin
    -- 迁移角色既不是超级用户也没有 BYPASSRLS，读行自证的断言会被 RLS 挡成空集而
    -- 假装通过。约束在 catalog 里，不受 RLS 影响，所以只断言列本身。
    if (select attnotnull from pg_attribute
         where attrelid = 'price_history'::regclass and attname = 'volume_basis') then
        raise exception 'volume_basis 仍然是 not null，口径未知的行插不进来';
    end if;

    -- 取值范围不能跟着放开：非空时仍然只有 single/double 两种。
    if not exists (
        select 1 from pg_constraint
         where conrelid = 'price_history'::regclass
           and conname = 'price_history_volume_basis_allowed'
    ) then
        raise exception '取值约束不见了，留空会变成什么都能填';
    end if;
end
$$;

insert into schema_versions (version, description)
values ('202608100010', 'Volume basis may be unknown when it cannot be measured')
on conflict (version) do nothing;

commit;

-- 让自研价差在库里以自己的身份落账。
--
-- 之前 `save_series` 把任何算完的序列都写成 provider_code='sanhe' 并挂上三禾的
-- data_sources 行。自研引擎接上来以后这就成了假账：我们自己用两腿收盘价算出来的
-- 东西，记录上写着是三禾给的。这里把 'self' 变成一等公民。
--
-- source_id 改成可空而不是给 'self' 造一条 data_sources：data_sources 建模的是
-- **外部**来源（带授权状态、连接器代码、白名单）。我们自己算的没有外部来源，
-- 硬造一条只会让那张表说谎。可空 + 「self 必须为空、其余必须非空」的双向约束，
-- 比造一条假来源诚实。

begin;

alter table spread_provider_series alter column source_id drop not null;

alter table spread_provider_series drop constraint spread_provider_series_provider;
alter table spread_provider_series add constraint spread_provider_series_provider
    check (provider_code in ('sanhe', 'self'));

-- 双向：自研的必须没有外部来源，外部的必须有。哪边写反了都插不进来。
alter table spread_provider_series add constraint spread_provider_series_source_presence
    check ((provider_code = 'self') = (source_id is null));

alter table spread_provider_series drop constraint spread_provider_series_price_basis;
-- 口径跟着来源走，不允许错配：三禾给的是算好的价差，我们给的是两腿收盘价相减。
alter table spread_provider_series add constraint spread_provider_series_price_basis
    check (
        (provider_code = 'sanhe' and price_basis = 'upstream_spread')
        or (provider_code = 'self' and price_basis = 'own_close_difference')
    );

alter table spread_favorites drop constraint spread_favorites_provider;
alter table spread_favorites add constraint spread_favorites_provider
    check (provider_code in ('sanhe', 'self'));

do $$
declare
    constraint_count integer;
begin
    -- 迁移角色既不是超级用户也没有 BYPASSRLS，凡是要读行来自证的断言都会被 RLS
    -- 挡成空集而假装通过。这里只断言约束本身，约束在 catalog 里，不受 RLS 影响。
    select count(*) into constraint_count
      from pg_constraint
     where conname in (
         'spread_provider_series_provider',
         'spread_provider_series_source_presence',
         'spread_provider_series_price_basis',
         'spread_favorites_provider'
     )
       and pg_get_constraintdef(oid) like '%self%';
    if constraint_count <> 4 then
        raise exception '自研 provider 的四条约束应当都提到 self，实际 %', constraint_count;
    end if;

    if (select attnotnull from pg_attribute
         where attrelid = 'spread_provider_series'::regclass and attname = 'source_id') then
        raise exception 'source_id 仍然是 not null，自研序列插不进来';
    end if;
end
$$;

commit;

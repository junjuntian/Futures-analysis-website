-- 两张历史表的交易所 check 约束放宽到 CFFEX/INE(DEC-158 第五处白名单)。
--
-- IH 全量装载 2026-08-30 深夜被 seat/price_history_exchange_allowed 拒:约束
-- 只放三家老交易所。当时数据流水线卡着、运营者等收工,**已在生产手工执行过
-- 与本迁移完全相同的语句**(begin/alter×4/commit,零数据风险、可回滚)——
-- 本迁移是追认,重跑幂等(先 drop 再 add,同名同定义)。
-- 加品种白名单清单(DEC-158)由五处更正为六处:席位装载白名单/行情脚本/
-- scope 表/点值两处/(跨所时)DEFAULT_EXCHANGES + **历史表交易所约束**。
begin;

alter table seat_history drop constraint seat_history_exchange_allowed;
alter table seat_history add constraint seat_history_exchange_allowed
    check (exchange in ('DCE', 'CZCE', 'SHFE', 'CFFEX', 'INE'));
alter table price_history drop constraint price_history_exchange_allowed;
alter table price_history add constraint price_history_exchange_allowed
    check (exchange in ('DCE', 'CZCE', 'SHFE', 'CFFEX', 'INE'));

insert into schema_versions (version, description)
values ('202608300002',
        'Allow CFFEX and INE in seat_history/price_history exchange checks (applied manually in prod 2026-08-30, ratified here)')
on conflict (version) do nothing;

commit;

-- 把新浪采到的大商所日行情装进 price_history。
--
-- 与 project-history.sql 分开是因为来路不同：那条是从 market_prices 投影（走审计导入
-- 通道采到的），这条是直接从新浪取的。两条都写 price_history，靠 source 分得清。
--
-- 幂等：按 (workspace_id, contract, trade_date, source) upsert，重复跑只刷新同一批行。

\set ON_ERROR_STOP on

begin;

create temp table dce_daily (
    exchange text, instrument text, contract text, trade_date date,
    open_price numeric, high_price numeric, low_price numeric, close_price numeric,
    settlement_price numeric, prev_settlement_price numeric,
    volume numeric, volume_basis text, turnover numeric,
    open_interest numeric, open_interest_change numeric, source text
);

\copy dce_daily from '/tmp/price_dce_daily.csv' with (format csv, header true, null '')

insert into price_history (
    id, workspace_id, exchange, instrument, contract, trade_date,
    open_price, high_price, low_price, close_price, settlement_price, prev_settlement_price,
    volume, volume_basis, turnover, open_interest, open_interest_change, source
)
select gen_random_uuid(), s.workspace_id, d.exchange, d.instrument, d.contract, d.trade_date,
       d.open_price, d.high_price, d.low_price, d.close_price, d.settlement_price,
       d.prev_settlement_price, d.volume, d.volume_basis, d.turnover,
       d.open_interest, d.open_interest_change, d.source
  from dce_daily d
  -- 只装两个产品覆盖的品种，且只装该 workspace 已经登记了范围的。
  join product_instrument_scope s
    on s.instrument = d.instrument and s.exchange = d.exchange
 where d.close_price is not null or d.settlement_price is not null
on conflict (workspace_id, contract, trade_date, source) do update set
    open_price = excluded.open_price,
    high_price = excluded.high_price,
    low_price = excluded.low_price,
    close_price = excluded.close_price,
    settlement_price = excluded.settlement_price,
    volume = excluded.volume,
    volume_basis = excluded.volume_basis,
    open_interest = excluded.open_interest,
    loaded_at = now();

commit;

select 'DCE 价格' as 表, max(trade_date) as 最新交易日, count(*) as 总行数
  from price_history where exchange = 'DCE';

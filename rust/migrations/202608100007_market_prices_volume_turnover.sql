begin;

-- The daily range and the traded totals, which the collector has been sending
-- and the projection has been dropping.
--
-- `202608100002` added open/high/low and the collector started emitting them
-- along with volume and turnover, but the projection that writes market_prices
-- was never taught to persist any of it. The columns existed and the rows kept
-- arriving, so nothing failed: 14,429 rows carry a close and a settlement and
-- not one carries an open. Two of the three products planned here need what was
-- being thrown away -- the candlestick panel of 建仓过程 is the range, and the
-- price-multiplier check is turnover / (volume x settlement).
--
-- Volume is in lots as the exchanges publish it,双边 for DCE. Turnover is in
-- yuan. Both are integers in every published file, but numeric is used because
-- an aggregate over thirteen years of turnover overflows bigint at these sizes.

alter table market_prices
    add column volume numeric(24, 8),
    add column turnover numeric(28, 8);

alter table market_prices
    add constraint market_prices_volume_not_negative check (
        volume is null or volume >= 0
    ),
    add constraint market_prices_turnover_not_negative check (
        turnover is null or turnover >= 0
    );

insert into schema_versions (version, description)
values ('202608100007', 'Volume and turnover on market_prices')
on conflict (version) do nothing;

commit;

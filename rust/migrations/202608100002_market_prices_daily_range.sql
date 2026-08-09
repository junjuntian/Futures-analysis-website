begin;

-- The daily price range, which the table has never carried.
--
-- Two things need it. The seat position-building view is a candlestick over
-- the contract, which cannot be drawn from a close and a settlement alone. And
-- the range is how a reader judges whether an estimated position cost is worth
-- trusting: a day that traded in a one-tick band pins the cost tightly, a day
-- that spanned the limit does not.
--
-- Nullable on purpose. Rows already loaded have no range and never will, and a
-- source that does not publish one — or a contract that did not trade that day
-- — must record its absence rather than a fabricated number. The exchange's own
-- files write 0 into all four fields for a contract with no volume, which is a
-- statement that nothing traded, not that it traded at zero; the collector
-- leaves those empty.
alter table market_prices
    add column open_price numeric(20, 8),
    add column high_price numeric(20, 8),
    add column low_price numeric(20, 8);

-- A range that is inside out would be silently wrong in every chart drawn from
-- it, so refuse it at the boundary instead.
alter table market_prices
    add constraint market_prices_daily_range_ordered check (
        (high_price is null or low_price is null or high_price >= low_price)
        and (open_price is null or low_price is null or open_price >= low_price)
        and (open_price is null or high_price is null or open_price <= high_price)
        and (close_price is null or low_price is null or close_price >= low_price)
        and (close_price is null or high_price is null or close_price <= high_price)
    );

insert into schema_versions (version, description)
values ('202608100002', 'Daily open/high/low on market_prices')
on conflict (version) do nothing;

commit;

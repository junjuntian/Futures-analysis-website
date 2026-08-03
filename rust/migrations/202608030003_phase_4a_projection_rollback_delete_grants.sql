begin;

-- Direct rollback removes formal projection rows created by an import batch.
-- Phase 4A originally granted the runtime role only read/write privileges,
-- so precheck could prove a rollback safe while the Worker could not execute
-- the required deletes.
grant delete on
    exchanges,
    instruments,
    contracts,
    trading_calendar_versions,
    trading_calendar_days,
    market_prices,
    seat_entities,
    seat_positions
to futures_runtime;

insert into schema_versions (version, description)
values ('202608030003', 'Phase 4A formal projection rollback delete grants')
on conflict (version) do nothing;

commit;

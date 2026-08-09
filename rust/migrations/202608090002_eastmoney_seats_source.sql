begin;

-- Eastmoney publishes the same member-level seat ranking the exchanges do, as
-- one report covering all five markets, and it is the only source that can
-- answer when an exchange's own seat file is unavailable. It is admitted as a
-- seats-only aggregator behind the official sources, never ahead of them, and
-- never for market data: its report carries no settlement price.
--
-- It does not reach the network through akshare, which has no Eastmoney futures
-- seat function, so it needs its own connector code rather than borrowing
-- `akshare_v1` and misreporting how the data was fetched.

alter table data_sources
    drop constraint data_sources_authorization_allowed,
    drop constraint data_sources_connector_allowed;

alter table data_sources
    add constraint data_sources_authorization_allowed check (
        (source_type = 'exchange_public'
            and authorization_status = 'whitelisted'
            and connector_code = 'akshare_v1')
        or
        (source_type = 'aggregator_public'
            and authorization_status = 'whitelisted_exception'
            and connector_code in ('akshare_v1', 'eastmoney_seats_v1'))
        or
        (source_type = 'aggregator'
            and authorization_status = 'user_authorized_readonly'
            and connector_code = 'sanhe_spread_v1')
    ),
    add constraint data_sources_connector_allowed check (
        connector_code in ('akshare_v1', 'sanhe_spread_v1', 'eastmoney_seats_v1')
    );

insert into schema_versions (version, description)
values ('202608090002', 'Eastmoney seats-only aggregator source')
on conflict (version) do nothing;

commit;

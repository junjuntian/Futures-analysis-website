begin;

-- The exchange's own annual history files. They are the authoritative record
-- for years no live endpoint will serve: DCE's API has answered 412 to every
-- client since 2026-08-02, Sina keeps delisted contracts only back to about
-- 2018-09, and Eastmoney keeps none at all. The files carry the settlement
-- price, which the one aggregator reaching that far back does not.
--
-- They are downloaded once through a browser by the operator, because the
-- site's WAF refuses scripted clients and defeating that is out of scope. The
-- collector reads them from disk and never opens a socket for this source, so
-- it gets its own connector code rather than borrowing one that implies a
-- network client.
--
-- It is an exchange source, not an aggregator: the bytes are the exchange's
-- own published files, unmodified.

alter table data_sources
    drop constraint data_sources_authorization_allowed,
    drop constraint data_sources_connector_allowed;

alter table data_sources
    add constraint data_sources_authorization_allowed check (
        (source_type = 'exchange_public'
            and authorization_status = 'whitelisted'
            and connector_code in ('akshare_v1', 'dce_history_files_v1'))
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
        connector_code in (
            'akshare_v1',
            'sanhe_spread_v1',
            'eastmoney_seats_v1',
            'dce_history_files_v1'
        )
    );

insert into schema_versions (version, description)
values ('202608100001', 'DCE official annual history file source')
on conflict (version) do nothing;

commit;

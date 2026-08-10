begin;

-- `DEC-045`: DCE's prices move to Eastmoney's quote endpoints.
--
-- The exchange's own API has answered HTTP 412 to every client since
-- 2026-08-02 (`DEC-041`), and the Sina fallback chosen at the time turned out
-- to have no history at all for 105 of the 186 contracts DCE listed on
-- 2026-08-07 -- including the 生猪 contracts this platform needs -- so it could
-- never complete a day and its completeness guard failed every collection.
--
-- `DEC-041` rejected Eastmoney for market data because it "lacks the settlement
-- price". That was true of the candlestick endpoint, which carries no
-- settlement for any instrument anywhere. The quote endpoint does carry it, in
-- field f130, checked against the SHFE official settlement for 2026-08-07 on
-- cu2609, cu2610, cu2612 and cu2703 -- all four equal.
--
-- It does not travel through akshare, which has no Eastmoney futures quote
-- function, so it gets its own connector code rather than borrowing
-- `akshare_v1` and misstating how the data was fetched. It is a distinct code
-- from `eastmoney_seats_v1`: different endpoints, different report, and one
-- must never be able to answer for the other.

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
            and connector_code in (
                'akshare_v1',
                'eastmoney_seats_v1',
                'eastmoney_dce_quote_v1'
            ))
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
            'dce_history_files_v1',
            'eastmoney_dce_quote_v1'
        )
    );

-- The retired sources keep their rows untouched. Batches collected through them
-- are part of the audit record and point at these ids; deleting them would
-- orphan real history in order to tidy up a list. What stops them being used is
-- that the collector no longer names them, not a flag here.

insert into schema_versions (version, description)
values ('202608100004', 'Eastmoney DCE market source, retiring the DCE official and Sina sources')
on conflict (version) do nothing;

commit;

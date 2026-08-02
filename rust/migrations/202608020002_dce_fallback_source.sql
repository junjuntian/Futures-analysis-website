begin;

alter table data_sources
    drop constraint data_sources_type_allowed,
    drop constraint data_sources_authorization_allowed;

alter table data_sources
    add constraint data_sources_type_allowed check (
        source_type in ('exchange_public', 'aggregator_public')
    ),
    add constraint data_sources_authorization_allowed check (
        (source_type = 'exchange_public' and authorization_status = 'whitelisted')
        or
        (source_type = 'aggregator_public' and authorization_status = 'whitelisted_exception')
    );

commit;

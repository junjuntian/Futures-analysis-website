begin;

update data_sources
   set priority = 50,
       updated_at = now()
 where code = 'akshare_sina_dce_fallback'
   and priority <> 50;

-- HIGH-02: automatic import identity follows the controlled source identity,
-- matching the source-aware formal fact keys. Existing Phase 4A records are
-- rewritten before new source-aware validation keys can be produced.
update imported_records record
   set business_key = upper(source.code) || '|' || record.business_key,
       updated_at = now()
  from import_batches batch
  join data_sources source
    on source.workspace_id = batch.workspace_id
   and source.id = batch.data_source_id
 where record.workspace_id = batch.workspace_id
   and record.source_import_batch_id = batch.id
   and batch.ingestion_mode = 'automatic'
   and record.business_key not like upper(source.code) || '|%';

update import_staging_rows staging
   set business_key = upper(source.code) || '|' || staging.business_key
  from import_batches batch
  join data_sources source
    on source.workspace_id = batch.workspace_id
   and source.id = batch.data_source_id
 where staging.workspace_id = batch.workspace_id
   and staging.import_batch_id = batch.id
   and batch.ingestion_mode = 'automatic'
   and staging.business_key is not null
   and staging.business_key not like upper(source.code) || '|%';

alter table trading_calendar_versions
    drop constraint trading_calendar_versions_business_identity,
    add constraint trading_calendar_versions_business_identity
        unique (workspace_id, exchange_id, source_id, version);

create index market_prices_preferred_lookup_idx
    on market_prices (workspace_id, contract_id, trade_date, session_type, granularity, revision_no);

create index seat_positions_preferred_lookup_idx
    on seat_positions (workspace_id, trade_date, contract_id, seat_id, rank_type, rank);

create view preferred_market_prices
with (security_invoker = true)
as
select *
  from (
        select price.*,
               row_number() over (
                   partition by price.workspace_id, price.contract_id, price.trade_date,
                                price.session_type, price.granularity, price.revision_no
                   order by source.priority desc, price.observed_at desc,
                            price.created_at desc, price.id desc
               ) as source_preference_rank
          from market_prices price
          join data_sources source
            on source.workspace_id = price.workspace_id
           and source.id = price.source_id
       ) ranked
 where source_preference_rank = 1;

create view preferred_seat_positions
with (security_invoker = true)
as
select *
  from (
        select position.*,
               row_number() over (
                   partition by position.workspace_id, position.trade_date,
                                position.contract_id, position.seat_id,
                                position.rank_type, position.rank
                   order by source.priority desc, position.created_at desc,
                            position.id desc
               ) as source_preference_rank
          from seat_positions position
          join data_sources source
            on source.workspace_id = position.workspace_id
           and source.id = position.source_id
       ) ranked
 where source_preference_rank = 1;

grant select on preferred_market_prices, preferred_seat_positions to futures_runtime;

-- HIGH-03: only change-log v2 automatic batches have a complete formal
-- projection chain. Existing Phase 4A batches were written with v1 logs that
-- contain imported_records only and therefore remain compensation-only.
update import_batches
   set rollback_capability = 'compensation_only',
       change_log_version = null
 where ingestion_mode = 'automatic'
   and rollback_capability = 'direct';

alter table import_row_changes
    drop constraint import_row_changes_target_kind_allowed,
    add constraint import_row_changes_target_kind_allowed check (
        target_kind in (
            'imported_record', 'exchange', 'instrument', 'contract',
            'trading_calendar_version', 'trading_calendar_day',
            'market_price', 'seat_entity', 'seat_position'
        )
    );

alter table import_rollback_conflicts
    drop constraint import_rollback_conflicts_target_kind_allowed,
    add constraint import_rollback_conflicts_target_kind_allowed check (
        target_kind is null or target_kind in (
            'imported_record', 'exchange', 'instrument', 'contract',
            'trading_calendar_version', 'trading_calendar_day',
            'market_price', 'seat_entity', 'seat_position'
        )
    );

alter table import_data_invalidations
    drop constraint import_data_invalidations_target_kind_allowed,
    add constraint import_data_invalidations_target_kind_allowed check (
        target_kind in (
            'imported_record', 'exchange', 'instrument', 'contract',
            'trading_calendar_version', 'trading_calendar_day',
            'market_price', 'seat_entity', 'seat_position'
        )
    );

alter table trading_calendar_versions
    add column row_version bigint not null default 1,
    add constraint trading_calendar_versions_row_version_positive check (row_version > 0);

alter table trading_calendar_days
    add column row_version bigint not null default 1,
    add constraint trading_calendar_days_row_version_positive check (row_version > 0);

alter table market_prices
    add column row_version bigint not null default 1,
    add constraint market_prices_row_version_positive check (row_version > 0);

alter table seat_positions
    add column row_version bigint not null default 1,
    add constraint seat_positions_row_version_positive check (row_version > 0);

commit;

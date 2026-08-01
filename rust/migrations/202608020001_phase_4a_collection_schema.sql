begin;

create table data_sources (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    code text not null,
    name text not null,
    source_type text not null,
    base_domain text not null,
    authorization_status text not null,
    connector_code text not null,
    priority integer not null default 100,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint data_sources_workspace_identity unique (workspace_id, id),
    constraint data_sources_business_identity unique (workspace_id, code),
    constraint data_sources_code_not_blank check (length(trim(code)) > 0),
    constraint data_sources_name_not_blank check (length(trim(name)) > 0),
    constraint data_sources_type_allowed check (source_type = 'exchange_public'),
    constraint data_sources_authorization_allowed check (authorization_status = 'whitelisted'),
    constraint data_sources_connector_allowed check (connector_code = 'akshare_v1'),
    constraint data_sources_domain_not_blank check (length(trim(base_domain)) > 0),
    constraint data_sources_priority_positive check (priority > 0)
);

create table data_source_allowed_domains (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    data_source_id uuid not null,
    domain text not null,
    created_at timestamptz not null default now(),
    constraint data_source_allowed_domains_business_identity
        unique (workspace_id, data_source_id, domain),
    constraint data_source_allowed_domains_source_fk foreign key (workspace_id, data_source_id)
        references data_sources(workspace_id, id) on delete cascade,
    constraint data_source_allowed_domains_domain_format check (
        domain = lower(domain) and domain ~ '^[a-z0-9.-]+$' and domain !~ '\.\.'
    )
);

create table exchanges (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    code text not null,
    name text not null,
    timezone text not null,
    source_record_id uuid not null,
    row_version bigint not null default 1,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint exchanges_workspace_identity unique (workspace_id, id),
    constraint exchanges_business_identity unique (workspace_id, code),
    constraint exchanges_source_record_identity unique (workspace_id, source_record_id),
    constraint exchanges_source_record_fk foreign key (workspace_id, source_record_id)
        references imported_records(workspace_id, id) on delete cascade,
    constraint exchanges_code_not_blank check (length(trim(code)) > 0),
    constraint exchanges_name_not_blank check (length(trim(name)) > 0),
    constraint exchanges_timezone_not_blank check (length(trim(timezone)) > 0),
    constraint exchanges_row_version_positive check (row_version > 0)
);

create table instruments (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    exchange_id uuid not null,
    code text not null,
    name text not null,
    currency_code char(3) not null,
    contract_multiplier numeric(20,8),
    price_tick numeric(20,8),
    source_record_id uuid not null,
    row_version bigint not null default 1,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint instruments_workspace_identity unique (workspace_id, id),
    constraint instruments_business_identity unique (workspace_id, exchange_id, code),
    constraint instruments_source_record_identity unique (workspace_id, source_record_id),
    constraint instruments_exchange_fk foreign key (workspace_id, exchange_id)
        references exchanges(workspace_id, id) on delete restrict,
    constraint instruments_source_record_fk foreign key (workspace_id, source_record_id)
        references imported_records(workspace_id, id) on delete cascade,
    constraint instruments_code_not_blank check (length(trim(code)) > 0),
    constraint instruments_name_not_blank check (length(trim(name)) > 0),
    constraint instruments_currency_upper check (currency_code ~ '^[A-Z]{3}$'),
    constraint instruments_multiplier_positive check (contract_multiplier is null or contract_multiplier > 0),
    constraint instruments_tick_positive check (price_tick is null or price_tick > 0),
    constraint instruments_row_version_positive check (row_version > 0)
);

create table contracts (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    instrument_id uuid not null,
    code text not null,
    delivery_month char(7),
    listed_at date,
    expires_at date,
    source_record_id uuid not null,
    row_version bigint not null default 1,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint contracts_workspace_identity unique (workspace_id, id),
    constraint contracts_business_identity unique (workspace_id, instrument_id, code),
    constraint contracts_code_workspace_identity unique (workspace_id, code),
    constraint contracts_source_record_identity unique (workspace_id, source_record_id),
    constraint contracts_instrument_fk foreign key (workspace_id, instrument_id)
        references instruments(workspace_id, id) on delete restrict,
    constraint contracts_source_record_fk foreign key (workspace_id, source_record_id)
        references imported_records(workspace_id, id) on delete cascade,
    constraint contracts_code_not_blank check (length(trim(code)) > 0),
    constraint contracts_delivery_month_format check (delivery_month is null or delivery_month ~ '^[0-9]{4}-[0-9]{2}$'),
    constraint contracts_date_order check (listed_at is null or expires_at is null or listed_at <= expires_at),
    constraint contracts_row_version_positive check (row_version > 0)
);

create table trading_calendar_versions (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    exchange_id uuid not null,
    version text not null,
    source_id uuid not null,
    effective_from date not null,
    created_by uuid not null references users(id) on delete restrict,
    source_record_id uuid not null,
    created_at timestamptz not null default now(),
    constraint trading_calendar_versions_workspace_identity unique (workspace_id, id),
    constraint trading_calendar_versions_business_identity unique (workspace_id, exchange_id, version),
    constraint trading_calendar_versions_source_record_identity unique (workspace_id, source_record_id),
    constraint trading_calendar_versions_exchange_fk foreign key (workspace_id, exchange_id)
        references exchanges(workspace_id, id) on delete restrict,
    constraint trading_calendar_versions_source_fk foreign key (workspace_id, source_id)
        references data_sources(workspace_id, id) on delete restrict,
    constraint trading_calendar_versions_source_record_fk foreign key (workspace_id, source_record_id)
        references imported_records(workspace_id, id) on delete cascade,
    constraint trading_calendar_versions_version_not_blank check (length(trim(version)) > 0)
);

create table trading_calendar_days (
    id bigint generated always as identity primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    calendar_version_id uuid not null,
    trade_date date not null,
    is_trading_day boolean not null,
    day_session_json jsonb not null default '{}'::jsonb,
    night_session_json jsonb not null default '{}'::jsonb,
    source_import_batch_id uuid not null,
    source_row_number integer not null,
    source_record_id uuid not null,
    created_at timestamptz not null default now(),
    constraint trading_calendar_days_business_identity unique (workspace_id, calendar_version_id, trade_date),
    constraint trading_calendar_days_source_record_identity unique (workspace_id, source_record_id),
    constraint trading_calendar_days_calendar_fk foreign key (workspace_id, calendar_version_id)
        references trading_calendar_versions(workspace_id, id) on delete restrict,
    constraint trading_calendar_days_batch_fk foreign key (workspace_id, source_import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint trading_calendar_days_source_record_fk foreign key (workspace_id, source_record_id)
        references imported_records(workspace_id, id) on delete cascade,
    constraint trading_calendar_days_sessions_objects check (
        jsonb_typeof(day_session_json) = 'object' and jsonb_typeof(night_session_json) = 'object'
    ),
    constraint trading_calendar_days_source_row_positive check (source_row_number > 0)
);

create table market_prices (
    id bigint generated always as identity primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    source_id uuid not null,
    contract_id uuid not null,
    trade_date date not null,
    session_type text not null,
    observed_at timestamptz not null,
    granularity text not null,
    close_price numeric(20,8),
    settlement_price numeric(20,8),
    currency_code char(3) not null,
    calendar_version_id uuid not null,
    revision_no integer not null default 1,
    source_import_batch_id uuid not null,
    source_row_number integer not null,
    source_record_id uuid not null,
    created_at timestamptz not null default now(),
    constraint market_prices_business_identity unique (
        workspace_id, source_id, contract_id, trade_date, session_type, granularity, revision_no
    ),
    constraint market_prices_source_record_identity unique (workspace_id, source_record_id),
    constraint market_prices_source_fk foreign key (workspace_id, source_id)
        references data_sources(workspace_id, id) on delete restrict,
    constraint market_prices_contract_fk foreign key (workspace_id, contract_id)
        references contracts(workspace_id, id) on delete restrict,
    constraint market_prices_calendar_fk foreign key (workspace_id, calendar_version_id)
        references trading_calendar_versions(workspace_id, id) on delete restrict,
    constraint market_prices_batch_fk foreign key (workspace_id, source_import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint market_prices_source_record_fk foreign key (workspace_id, source_record_id)
        references imported_records(workspace_id, id) on delete cascade,
    constraint market_prices_session_allowed check (session_type = 'daily'),
    constraint market_prices_granularity_allowed check (granularity = '1d'),
    constraint market_prices_price_present check (close_price is not null or settlement_price is not null),
    constraint market_prices_currency_upper check (currency_code ~ '^[A-Z]{3}$'),
    constraint market_prices_revision_positive check (revision_no > 0),
    constraint market_prices_source_row_positive check (source_row_number > 0)
);

create table seat_entities (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    canonical_name text not null,
    status text not null default 'unreviewed',
    source_record_id uuid not null,
    row_version bigint not null default 1,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint seat_entities_workspace_identity unique (workspace_id, id),
    constraint seat_entities_business_identity unique (workspace_id, canonical_name),
    constraint seat_entities_source_record_identity unique (workspace_id, source_record_id),
    constraint seat_entities_source_record_fk foreign key (workspace_id, source_record_id)
        references imported_records(workspace_id, id) on delete cascade,
    constraint seat_entities_name_not_blank check (length(trim(canonical_name)) > 0),
    constraint seat_entities_status_allowed check (status in ('unreviewed', 'active', 'merged', 'inactive')),
    constraint seat_entities_row_version_positive check (row_version > 0)
);

create table seat_positions (
    id bigint generated always as identity primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    trade_date date not null,
    contract_id uuid not null,
    seat_id uuid not null,
    rank_type text not null,
    rank integer not null,
    volume bigint,
    long_position bigint,
    short_position bigint,
    source_id uuid not null,
    source_import_batch_id uuid not null,
    source_row_number integer not null,
    source_record_id uuid not null,
    created_at timestamptz not null default now(),
    constraint seat_positions_business_identity unique (
        workspace_id, source_id, trade_date, contract_id, seat_id, rank_type, rank
    ),
    constraint seat_positions_source_record_identity unique (workspace_id, source_record_id),
    constraint seat_positions_contract_fk foreign key (workspace_id, contract_id)
        references contracts(workspace_id, id) on delete restrict,
    constraint seat_positions_seat_fk foreign key (workspace_id, seat_id)
        references seat_entities(workspace_id, id) on delete restrict,
    constraint seat_positions_source_fk foreign key (workspace_id, source_id)
        references data_sources(workspace_id, id) on delete restrict,
    constraint seat_positions_batch_fk foreign key (workspace_id, source_import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint seat_positions_source_record_fk foreign key (workspace_id, source_record_id)
        references imported_records(workspace_id, id) on delete cascade,
    constraint seat_positions_rank_type_allowed check (rank_type in ('volume', 'long', 'short')),
    constraint seat_positions_rank_positive check (rank > 0),
    constraint seat_positions_values_nonnegative check (
        (volume is null or volume >= 0) and
        (long_position is null or long_position >= 0) and
        (short_position is null or short_position >= 0)
    ),
    constraint seat_positions_rank_payload check (
        (rank_type = 'volume' and volume is not null and long_position is null and short_position is null)
        or (rank_type = 'long' and volume is null and long_position is not null and short_position is null)
        or (rank_type = 'short' and volume is null and long_position is null and short_position is not null)
    ),
    constraint seat_positions_source_row_positive check (source_row_number > 0)
);

create table extraction_jobs (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    data_source_id uuid not null,
    import_batch_id uuid,
    status text not null,
    dataset_type text not null,
    collection_scope_json jsonb not null,
    output_object_id uuid,
    stable_error_code text,
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    constraint extraction_jobs_workspace_identity unique (workspace_id, id),
    constraint extraction_jobs_source_fk foreign key (workspace_id, data_source_id)
        references data_sources(workspace_id, id) on delete restrict,
    constraint extraction_jobs_batch_fk foreign key (workspace_id, import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint extraction_jobs_object_fk foreign key (workspace_id, output_object_id)
        references stored_objects(workspace_id, id) on delete restrict,
    constraint extraction_jobs_status_allowed check (status in ('uploaded', 'queued', 'running', 'succeeded', 'failed')),
    constraint extraction_jobs_dataset_allowed check (dataset_type in (
        'futures_catalog_v1', 'trading_calendar_v1', 'daily_market_prices_v1', 'seat_positions_v1'
    )),
    constraint extraction_jobs_scope_object check (jsonb_typeof(collection_scope_json) = 'object'),
    constraint extraction_jobs_completion check (
        (status in ('uploaded', 'queued', 'running') and completed_at is null)
        or (status in ('succeeded', 'failed') and completed_at is not null)
    )
);

alter table import_batches
    add column ingestion_mode text not null default 'manual',
    add column data_source_id uuid,
    add column collection_date date,
    add column fixed_template_code text,
    add constraint import_batches_data_source_fk foreign key (workspace_id, data_source_id)
        references data_sources(workspace_id, id) on delete restrict,
    add constraint import_batches_ingestion_mode_allowed check (ingestion_mode in ('manual', 'automatic')),
    add constraint import_batches_automatic_metadata check (
        (ingestion_mode = 'manual' and data_source_id is null and collection_date is null and fixed_template_code is null)
        or (
            ingestion_mode = 'automatic' and data_source_id is not null and collection_date is not null
            and fixed_template_code = dataset_type || '@1'
            and dataset_type in ('futures_catalog_v1', 'trading_calendar_v1', 'daily_market_prices_v1', 'seat_positions_v1')
        )
    );

create index data_sources_workspace_domain_idx on data_sources (workspace_id, base_domain);
create index contracts_workspace_code_idx on contracts (workspace_id, code);
create index trading_calendar_days_workspace_date_idx on trading_calendar_days (workspace_id, trade_date);
create index market_prices_workspace_date_idx on market_prices (workspace_id, trade_date, contract_id);
create index market_prices_workspace_batch_idx on market_prices (workspace_id, source_import_batch_id);
create index seat_positions_workspace_date_idx on seat_positions (workspace_id, trade_date, contract_id);
create index seat_positions_workspace_batch_idx on seat_positions (workspace_id, source_import_batch_id);
create index extraction_jobs_workspace_started_idx on extraction_jobs (workspace_id, started_at desc);
create index import_batches_workspace_automatic_idx on import_batches (workspace_id, collection_date, data_source_id, dataset_type)
    where ingestion_mode = 'automatic';

alter table data_sources enable row level security;
alter table data_sources force row level security;
alter table data_source_allowed_domains enable row level security;
alter table data_source_allowed_domains force row level security;
alter table exchanges enable row level security;
alter table exchanges force row level security;
alter table instruments enable row level security;
alter table instruments force row level security;
alter table contracts enable row level security;
alter table contracts force row level security;
alter table trading_calendar_versions enable row level security;
alter table trading_calendar_versions force row level security;
alter table trading_calendar_days enable row level security;
alter table trading_calendar_days force row level security;
alter table market_prices enable row level security;
alter table market_prices force row level security;
alter table seat_entities enable row level security;
alter table seat_entities force row level security;
alter table seat_positions enable row level security;
alter table seat_positions force row level security;
alter table extraction_jobs enable row level security;
alter table extraction_jobs force row level security;

create policy data_sources_workspace_isolation on data_sources using (workspace_id = app.current_workspace_id()) with check (workspace_id = app.current_workspace_id());
create policy data_source_allowed_domains_workspace_isolation on data_source_allowed_domains using (workspace_id = app.current_workspace_id()) with check (workspace_id = app.current_workspace_id());
create policy exchanges_workspace_isolation on exchanges using (workspace_id = app.current_workspace_id()) with check (workspace_id = app.current_workspace_id());
create policy instruments_workspace_isolation on instruments using (workspace_id = app.current_workspace_id()) with check (workspace_id = app.current_workspace_id());
create policy contracts_workspace_isolation on contracts using (workspace_id = app.current_workspace_id()) with check (workspace_id = app.current_workspace_id());
create policy trading_calendar_versions_workspace_isolation on trading_calendar_versions using (workspace_id = app.current_workspace_id()) with check (workspace_id = app.current_workspace_id());
create policy trading_calendar_days_workspace_isolation on trading_calendar_days using (workspace_id = app.current_workspace_id()) with check (workspace_id = app.current_workspace_id());
create policy market_prices_workspace_isolation on market_prices using (workspace_id = app.current_workspace_id()) with check (workspace_id = app.current_workspace_id());
create policy seat_entities_workspace_isolation on seat_entities using (workspace_id = app.current_workspace_id()) with check (workspace_id = app.current_workspace_id());
create policy seat_positions_workspace_isolation on seat_positions using (workspace_id = app.current_workspace_id()) with check (workspace_id = app.current_workspace_id());
create policy extraction_jobs_workspace_isolation on extraction_jobs using (workspace_id = app.current_workspace_id()) with check (workspace_id = app.current_workspace_id());

grant select, insert, update on data_sources, data_source_allowed_domains,
    exchanges, instruments, contracts,
    trading_calendar_versions, trading_calendar_days, market_prices, seat_entities,
    seat_positions, extraction_jobs to futures_runtime;
grant usage, select on all sequences in schema public to futures_runtime;

insert into schema_versions (version, description)
values ('202608020001', 'phase 4a akshare collection business schema')
on conflict (version) do nothing;

commit;

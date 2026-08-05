begin;

alter table data_sources
    drop constraint data_sources_type_allowed,
    drop constraint data_sources_authorization_allowed,
    drop constraint data_sources_connector_allowed;

alter table data_sources
    add constraint data_sources_type_allowed check (
        source_type in ('exchange_public', 'aggregator_public', 'aggregator')
    ),
    add constraint data_sources_authorization_allowed check (
        (source_type = 'exchange_public'
            and authorization_status = 'whitelisted'
            and connector_code = 'akshare_v1')
        or
        (source_type = 'aggregator_public'
            and authorization_status = 'whitelisted_exception'
            and connector_code = 'akshare_v1')
        or
        (source_type = 'aggregator'
            and authorization_status = 'user_authorized_readonly'
            and connector_code = 'sanhe_spread_v1')
    ),
    add constraint data_sources_connector_allowed check (
        connector_code in ('akshare_v1', 'sanhe_spread_v1')
    );

create table spread_provider_cache (
    id uuid primary key,
    provider_code text not null,
    endpoint_code text not null,
    parameter_hash char(64) not null,
    parameters_json jsonb not null,
    business_date date not null,
    fetched_at timestamptz not null,
    http_status integer not null,
    business_code integer not null,
    payload_json jsonb not null,
    result_kind text not null,
    payload_hash char(64) not null,
    created_at timestamptz not null default now(),
    constraint spread_provider_cache_identity unique (
        provider_code, endpoint_code, parameter_hash, business_date
    ),
    constraint spread_provider_cache_provider_not_blank
        check (provider_code = 'sanhe'),
    constraint spread_provider_cache_endpoint_allowed check (
        endpoint_code in ('all_varieties', 'variety_contracts', 'arbitrage_varieties')
    ),
    constraint spread_provider_cache_parameters_object
        check (jsonb_typeof(parameters_json) = 'object'),
    constraint spread_provider_cache_payload_object
        check (jsonb_typeof(payload_json) = 'object'),
    constraint spread_provider_cache_hashes check (
        parameter_hash ~ '^[0-9a-f]{64}$' and payload_hash ~ '^[0-9a-f]{64}$'
    ),
    constraint spread_provider_cache_http_success
        check (http_status between 200 and 299),
    constraint spread_provider_cache_business_success check (business_code = 0),
    constraint spread_provider_cache_result_kind check (result_kind in ('ok', 'empty'))
);

create table spread_provider_throttles (
    provider_code text primary key,
    last_requested_at timestamptz,
    suppressed_until timestamptz,
    updated_at timestamptz not null default now(),
    constraint spread_provider_throttles_provider_not_blank
        check (provider_code = 'sanhe')
);

create table spread_provider_failures (
    provider_code text not null,
    endpoint_code text not null,
    parameter_hash char(64) not null,
    stable_error_code text not null,
    occurred_at timestamptz not null,
    suppressed_until timestamptz not null,
    updated_at timestamptz not null default now(),
    primary key (provider_code, endpoint_code, parameter_hash),
    constraint spread_provider_failures_provider check (provider_code = 'sanhe'),
    constraint spread_provider_failures_parameter_hash
        check (parameter_hash ~ '^[0-9a-f]{64}$'),
    constraint spread_provider_failures_endpoint_allowed check (
        endpoint_code in ('all_varieties', 'variety_contracts', 'arbitrage_varieties')
    ),
    constraint spread_provider_failures_error_allowed check (
        stable_error_code in (
            'spread_provider_unavailable',
            'spread_provider_rate_limited',
            'spread_provider_forbidden',
            'spread_provider_contract_changed'
        )
    ),
    constraint spread_provider_failures_interval
        check (suppressed_until > occurred_at)
);

create table retail_trade_window_rule_versions (
    id uuid primary key,
    version text not null unique,
    algorithm_version text not null,
    status text not null,
    effective_from date not null,
    effective_to date,
    created_at timestamptz not null default now(),
    constraint retail_window_version_not_blank
        check (length(trim(version)) > 0 and length(trim(algorithm_version)) > 0),
    constraint retail_window_version_status check (status in ('active', 'retired')),
    constraint retail_window_version_dates
        check (effective_to is null or effective_to >= effective_from)
);

create table retail_trade_window_rules (
    id uuid primary key,
    rule_version_id uuid not null references retail_trade_window_rule_versions(id) on delete restrict,
    exchange_code text,
    instrument_code text,
    rule_json jsonb not null,
    priority integer not null default 100,
    created_at timestamptz not null default now(),
    constraint retail_window_rule_selector_present
        check (exchange_code is not null or instrument_code is not null),
    constraint retail_window_rule_selectors_not_blank check (
        (exchange_code is null or length(trim(exchange_code)) > 0)
        and (instrument_code is null or length(trim(instrument_code)) > 0)
    ),
    constraint retail_window_rule_exchange_upper
        check (exchange_code is null or exchange_code = upper(exchange_code)),
    constraint retail_window_rule_instrument_upper
        check (instrument_code is null or instrument_code = upper(instrument_code)),
    constraint retail_window_rule_json_contract check (
        jsonb_typeof(rule_json) = 'object'
        and rule_json ->> 'kind' = 'relative_delivery_month_trading_day'
        and rule_json ->> 'month_offset' ~ '^-?[0-9]+$'
        and (rule_json ->> 'month_offset')::integer between -24 and 24
        and rule_json ->> 'trading_day_ordinal' ~ '^-?[0-9]+$'
        and (rule_json ->> 'trading_day_ordinal')::integer between -31 and 31
        and (rule_json ->> 'trading_day_ordinal')::integer <> 0
    ),
    constraint retail_window_rule_priority_positive check (priority > 0),
    constraint retail_window_rule_identity unique nulls not distinct (
        rule_version_id, exchange_code, instrument_code
    )
);

create table spread_provider_series (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    provider_code text not null,
    source_id uuid not null,
    query_hash char(64) not null,
    business_date date not null,
    query_json jsonb not null,
    fetched_at timestamptz not null,
    data_cutoff_at date,
    payload_hash char(64) not null,
    derivation_hash char(64) not null,
    price_basis text not null,
    window_algorithm_version text not null,
    statistics_algorithm_version text not null,
    rule_version text not null,
    created_by uuid not null references users(id) on delete restrict,
    created_at timestamptz not null default now(),
    constraint spread_provider_series_workspace_identity unique (workspace_id, id),
    constraint spread_provider_series_business_identity unique (
        workspace_id, provider_code, query_hash, business_date, derivation_hash
    ),
    constraint spread_provider_series_source_fk foreign key (workspace_id, source_id)
        references data_sources(workspace_id, id) on delete restrict,
    constraint spread_provider_series_provider check (provider_code = 'sanhe'),
    constraint spread_provider_series_hashes check (
        query_hash ~ '^[0-9a-f]{64}$'
        and payload_hash ~ '^[0-9a-f]{64}$'
        and derivation_hash ~ '^[0-9a-f]{64}$'
    ),
    constraint spread_provider_series_query_object check (jsonb_typeof(query_json) = 'object'),
    constraint spread_provider_series_price_basis check (price_basis = 'upstream_spread')
);

create table spread_provider_observations (
    id bigint generated always as identity primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    series_id uuid not null,
    point_seq integer not null,
    trade_date date not null,
    spread_value numeric(20,8) not null,
    from_code text not null,
    to_code text not null,
    segment_no integer,
    retained boolean not null,
    exclusion_reason text,
    created_at timestamptz not null default now(),
    constraint spread_provider_observations_series_fk foreign key (workspace_id, series_id)
        references spread_provider_series(workspace_id, id) on delete cascade,
    constraint spread_provider_observations_identity unique (workspace_id, series_id, point_seq),
    constraint spread_provider_observations_point_seq_positive check (point_seq > 0),
    constraint spread_provider_observations_segment_positive
        check (segment_no is null or segment_no > 0),
    constraint spread_provider_observations_codes_not_blank check (
        length(trim(from_code)) > 0 and length(trim(to_code)) > 0
    ),
    constraint spread_provider_observations_exclusion check (
        (retained and exclusion_reason is null and segment_no is not null)
        or (not retained and exclusion_reason is not null)
    ),
    constraint spread_provider_observations_exclusion_allowed check (
        exclusion_reason is null or exclusion_reason in (
            'contract_metadata_missing', 'outside_retail_window', 'empty_retail_window'
        )
    )
);

create table spread_window_segments (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    series_id uuid not null,
    segment_no integer not null,
    window_year integer,
    from_code text not null,
    to_code text not null,
    candidate_start date not null,
    candidate_end date not null,
    window_start date,
    window_end date,
    rule_version text not null,
    calendar_version_ids uuid[] not null default '{}',
    retained_point_count integer not null default 0,
    excluded_point_count integer not null default 0,
    boundary_reason text not null,
    created_at timestamptz not null default now(),
    constraint spread_window_segments_series_fk foreign key (workspace_id, series_id)
        references spread_provider_series(workspace_id, id) on delete cascade,
    constraint spread_window_segments_identity unique (workspace_id, series_id, segment_no),
    constraint spread_window_segments_number_positive check (segment_no > 0),
    constraint spread_window_segments_year_valid
        check (window_year is null or window_year between 2000 and 2200),
    constraint spread_window_segments_candidate_dates check (candidate_end >= candidate_start),
    constraint spread_window_segments_window_dates check (
        window_start is null or window_end is null or window_end >= window_start
    ),
    constraint spread_window_segments_counts_nonnegative check (
        retained_point_count >= 0 and excluded_point_count >= 0
    ),
    constraint spread_window_segments_boundary_reason check (
        boundary_reason in ('retail_deadline', 'contract_metadata_missing', 'empty_retail_window')
    )
);

create table spread_favorites (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    name text not null,
    provider_code text not null,
    leg1_json jsonb not null,
    leg2_json jsonb not null,
    normalized_hash char(64) not null,
    created_by uuid not null references users(id) on delete restrict,
    created_at timestamptz not null default now(),
    constraint spread_favorites_workspace_identity unique (workspace_id, id),
    constraint spread_favorites_business_identity unique (workspace_id, normalized_hash),
    constraint spread_favorites_normalized_hash
        check (normalized_hash ~ '^[0-9a-f]{64}$'),
    constraint spread_favorites_name_not_blank check (length(trim(name)) between 1 and 80),
    constraint spread_favorites_provider check (provider_code = 'sanhe'),
    constraint spread_favorites_legs_objects
        check (jsonb_typeof(leg1_json) = 'object' and jsonb_typeof(leg2_json) = 'object')
);

create index spread_provider_cache_lookup_idx on spread_provider_cache (
    provider_code, endpoint_code, business_date, parameter_hash
);
create index spread_provider_failures_suppression_idx on spread_provider_failures (
    provider_code, suppressed_until
);
create index spread_provider_series_workspace_date_idx on spread_provider_series (
    workspace_id, business_date desc
);
create index spread_provider_observations_series_date_idx on spread_provider_observations (
    workspace_id, series_id, trade_date
);
create index spread_favorites_workspace_created_idx on spread_favorites (
    workspace_id, created_at desc
);

alter table spread_provider_series enable row level security;
alter table spread_provider_series force row level security;
alter table spread_provider_observations enable row level security;
alter table spread_provider_observations force row level security;
alter table spread_window_segments enable row level security;
alter table spread_window_segments force row level security;
alter table spread_favorites enable row level security;
alter table spread_favorites force row level security;

create policy spread_provider_series_workspace_isolation on spread_provider_series
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());
create policy spread_provider_observations_workspace_isolation on spread_provider_observations
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());
create policy spread_window_segments_workspace_isolation on spread_window_segments
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());
create policy spread_favorites_workspace_isolation on spread_favorites
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

grant select, insert, update on spread_provider_cache, spread_provider_throttles to futures_runtime;
grant select, insert, update, delete on spread_provider_failures to futures_runtime;
grant select on retail_trade_window_rule_versions, retail_trade_window_rules to futures_runtime;
grant select, insert on spread_provider_series, spread_provider_observations,
    spread_window_segments to futures_runtime;
grant select, insert, delete on spread_favorites to futures_runtime;
grant usage, select on sequence spread_provider_observations_id_seq to futures_runtime;

insert into retail_trade_window_rule_versions
    (id, version, algorithm_version, status, effective_from)
values (
    '019c2ad8-e000-7000-8000-000000000001'::uuid,
    'retail-window-default-v1',
    'retail_window_v1',
    'active',
    '2026-08-05'
)
on conflict (version) do nothing;

insert into spread_provider_throttles (provider_code)
values ('sanhe')
on conflict (provider_code) do nothing;

insert into schema_versions (version, description)
values ('202608050001', 'Phase 5A Sanhe spread provider, tradable window, and favorites')
on conflict (version) do nothing;

commit;

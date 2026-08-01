begin;

create table job_queue (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    job_type text not null,
    aggregate_id uuid not null,
    status text not null default 'queued',
    payload jsonb not null,
    attempt_count integer not null default 0,
    max_attempts integer not null default 5,
    available_at timestamptz not null default now(),
    leased_by text,
    lease_expires_at timestamptz,
    lease_generation bigint not null default 0,
    last_error_code text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    finished_at timestamptz,
    constraint job_queue_workspace_identity unique (workspace_id, id),
    constraint job_queue_import_batch_fk
        foreign key (workspace_id, aggregate_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint job_queue_type_not_blank check (length(trim(job_type)) > 0),
    constraint job_queue_status_allowed
        check (status in ('queued', 'running', 'succeeded', 'failed', 'dead_letter')),
    constraint job_queue_payload_object check (jsonb_typeof(payload) = 'object'),
    constraint job_queue_attempts_valid
        check (
            attempt_count >= 0
            and max_attempts > 0
            and attempt_count <= max_attempts
        ),
    constraint job_queue_lease_generation_nonnegative check (lease_generation >= 0),
    constraint job_queue_lease_pair
        check (
            (leased_by is null and lease_expires_at is null)
            or (leased_by is not null and lease_expires_at is not null)
        ),
    constraint job_queue_running_has_lease
        check (
            status <> 'running'
            or (leased_by is not null and lease_expires_at is not null)
        )
);

create unique index job_queue_import_confirm_identity
    on job_queue (workspace_id, job_type, aggregate_id)
    where job_type = 'import_confirm';

create index job_queue_workspace_claim_idx
    on job_queue (workspace_id, status, available_at, lease_expires_at, created_at);

create table import_confirmations (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    import_batch_id uuid not null,
    idempotency_key_hash char(64) not null,
    request_hash char(64) not null,
    job_id uuid not null,
    confirmed_by uuid not null references users(id) on delete restrict,
    created_at timestamptz not null default now(),
    constraint import_confirmations_workspace_identity unique (workspace_id, id),
    constraint import_confirmations_idempotency_identity
        unique (workspace_id, idempotency_key_hash),
    constraint import_confirmations_batch_fk
        foreign key (workspace_id, import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint import_confirmations_job_fk
        foreign key (workspace_id, job_id)
        references job_queue(workspace_id, id) on delete restrict,
    constraint import_confirmations_idempotency_hash_hex
        check (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    constraint import_confirmations_request_hash_hex
        check (request_hash ~ '^[0-9a-f]{64}$')
);

create index import_confirmations_workspace_batch_idx
    on import_confirmations (workspace_id, import_batch_id, created_at, id);

create table import_job_events (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    import_batch_id uuid not null,
    job_id uuid not null,
    event_seq bigint not null,
    event_type text not null,
    payload jsonb not null,
    created_at timestamptz not null default now(),
    constraint import_job_events_workspace_identity unique (workspace_id, id),
    constraint import_job_events_batch_sequence
        unique (workspace_id, import_batch_id, event_seq),
    constraint import_job_events_batch_fk
        foreign key (workspace_id, import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint import_job_events_job_fk
        foreign key (workspace_id, job_id)
        references job_queue(workspace_id, id) on delete restrict,
    constraint import_job_events_sequence_positive check (event_seq > 0),
    constraint import_job_events_type_allowed
        check (
            event_type in (
                'queued',
                'running',
                'progress',
                'succeeded',
                'failed',
                'dead_letter'
            )
        ),
    constraint import_job_events_payload_object check (jsonb_typeof(payload) = 'object')
);

create index import_job_events_workspace_replay_idx
    on import_job_events (workspace_id, import_batch_id, event_seq);

alter table job_queue enable row level security;
alter table job_queue force row level security;
alter table import_confirmations enable row level security;
alter table import_confirmations force row level security;
alter table import_job_events enable row level security;
alter table import_job_events force row level security;

create policy job_queue_workspace_isolation on job_queue
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy import_confirmations_workspace_isolation on import_confirmations
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy import_job_events_workspace_isolation on import_job_events
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

grant select, insert, update on job_queue to futures_runtime;
grant select, insert on import_confirmations to futures_runtime;
grant select, insert on import_job_events to futures_runtime;

insert into schema_versions (version, description)
values ('202607250009', 'phase 3c job queue and import events')
on conflict (version) do nothing;

commit;

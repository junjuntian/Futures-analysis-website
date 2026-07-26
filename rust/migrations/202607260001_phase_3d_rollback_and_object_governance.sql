begin;

alter table import_batches
    add column rollback_capability text not null default 'compensation_only',
    add column change_log_version integer,
    add column compensates_batch_id uuid,
    add column rolled_back_at timestamptz,
    add constraint import_batches_rollback_capability_allowed
        check (rollback_capability in ('compensation_only', 'direct')),
    add constraint import_batches_change_log_version_positive
        check (change_log_version is null or change_log_version > 0),
    add constraint import_batches_rollback_capability_binding
        check (
            (rollback_capability = 'compensation_only' and change_log_version is null)
            or (rollback_capability = 'direct' and change_log_version is not null)
        ),
    add constraint import_batches_compensation_not_self
        check (compensates_batch_id is null or compensates_batch_id <> id),
    add constraint import_batches_compensation_workspace_fk
        foreign key (workspace_id, compensates_batch_id)
        references import_batches(workspace_id, id) on delete restrict;

-- Existing Phase 3C batches do not have a complete change log. The default
-- above deliberately marks every existing row as compensation-only; this
-- migration must never synthesize import_row_changes from current state.
update import_batches
   set rollback_capability = 'compensation_only',
       change_log_version = null;

create index import_batches_workspace_compensation_idx
    on import_batches (workspace_id, compensates_batch_id, created_at)
    where compensates_batch_id is not null;

create index import_batches_workspace_rollback_capability_idx
    on import_batches (workspace_id, rollback_capability, status, committed_at);

alter table import_files
    add constraint import_files_workspace_file_batch_identity
        unique (workspace_id, id, import_batch_id);

create table import_row_changes (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    import_batch_id uuid not null,
    sequence_no bigint not null,
    target_kind text not null,
    target_id uuid not null,
    operation text not null,
    before_json jsonb,
    after_json jsonb,
    target_row_version bigint not null,
    source_file_id uuid not null,
    source_row_number integer not null,
    created_at timestamptz not null default now(),
    constraint import_row_changes_workspace_identity unique (workspace_id, id),
    constraint import_row_changes_batch_sequence
        unique (workspace_id, import_batch_id, sequence_no),
    constraint import_row_changes_batch_fk
        foreign key (workspace_id, import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint import_row_changes_source_file_batch_fk
        foreign key (workspace_id, source_file_id, import_batch_id)
        references import_files(workspace_id, id, import_batch_id) on delete restrict,
    constraint import_row_changes_sequence_positive check (sequence_no > 0),
    constraint import_row_changes_target_kind_allowed
        check (target_kind = 'imported_record'),
    constraint import_row_changes_operation_allowed
        check (operation in ('insert', 'update', 'soft_delete')),
    constraint import_row_changes_json_objects
        check (
            (before_json is null or jsonb_typeof(before_json) = 'object')
            and (after_json is null or jsonb_typeof(after_json) = 'object')
        ),
    constraint import_row_changes_operation_payload
        check (
            (operation = 'insert' and before_json is null and after_json is not null)
            or (operation = 'update' and before_json is not null and after_json is not null)
            or (operation = 'soft_delete' and before_json is not null and after_json is not null)
        ),
    constraint import_row_changes_target_version_positive check (target_row_version > 0),
    constraint import_row_changes_source_row_positive check (source_row_number > 0)
);

create index import_row_changes_workspace_batch_reverse_idx
    on import_row_changes (workspace_id, import_batch_id, sequence_no desc);

create index import_row_changes_workspace_target_idx
    on import_row_changes (
        workspace_id,
        target_kind,
        target_id,
        import_batch_id,
        sequence_no
    );

create index import_row_changes_workspace_source_idx
    on import_row_changes (
        workspace_id,
        source_file_id,
        source_row_number,
        import_batch_id
    );

alter table job_queue
    add constraint job_queue_workspace_job_batch_identity
        unique (workspace_id, id, aggregate_id);

create table import_rollback_requests (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    import_batch_id uuid not null,
    requested_by uuid not null references users(id) on delete restrict,
    idempotency_key_hash char(64),
    request_hash char(64),
    precheck_fingerprint char(64) not null,
    status text not null default 'prechecked',
    conflict_count integer not null default 0,
    job_id uuid,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    finished_at timestamptz,
    constraint import_rollback_requests_workspace_identity unique (workspace_id, id),
    constraint import_rollback_requests_batch_identity
        unique (workspace_id, id, import_batch_id),
    constraint import_rollback_requests_idempotency_identity
        unique (workspace_id, idempotency_key_hash),
    constraint import_rollback_requests_batch_fk
        foreign key (workspace_id, import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint import_rollback_requests_job_batch_fk
        foreign key (workspace_id, job_id, import_batch_id)
        references job_queue(workspace_id, id, aggregate_id) on delete restrict,
    constraint import_rollback_requests_hashes_hex
        check (
            (idempotency_key_hash is null or idempotency_key_hash ~ '^[0-9a-f]{64}$')
            and (request_hash is null or request_hash ~ '^[0-9a-f]{64}$')
            and precheck_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    constraint import_rollback_requests_idempotency_pair
        check (
            (idempotency_key_hash is null and request_hash is null)
            or (idempotency_key_hash is not null and request_hash is not null)
        ),
    constraint import_rollback_requests_status_allowed
        check (
            status in (
                'prechecked',
                'precheck_conflict',
                'queued',
                'running',
                'succeeded',
                'worker_conflict',
                'failed'
            )
        ),
    constraint import_rollback_requests_conflict_count_nonnegative
        check (conflict_count >= 0),
    constraint import_rollback_requests_job_binding
        check (
            (
                status = 'prechecked'
                and conflict_count = 0
                and idempotency_key_hash is null
                and request_hash is null
                and job_id is null
                and finished_at is not null
            )
            or (
                status = 'precheck_conflict'
                and conflict_count > 0
                and idempotency_key_hash is null
                and request_hash is null
                and job_id is null
                and finished_at is not null
            )
            or (
                status in ('queued', 'running')
                and conflict_count = 0
                and idempotency_key_hash is not null
                and request_hash is not null
                and job_id is not null
                and finished_at is null
            )
            or (
                status = 'succeeded'
                and conflict_count = 0
                and idempotency_key_hash is not null
                and request_hash is not null
                and job_id is not null
                and finished_at is not null
            )
            or (
                status = 'worker_conflict'
                and conflict_count > 0
                and idempotency_key_hash is not null
                and request_hash is not null
                and job_id is not null
                and finished_at is not null
            )
            or (
                status = 'failed'
                and idempotency_key_hash is not null
                and request_hash is not null
                and job_id is not null
                and finished_at is not null
            )
        )
);

create index import_rollback_requests_workspace_batch_idx
    on import_rollback_requests (workspace_id, import_batch_id, created_at desc, id);

create unique index import_rollback_requests_one_active_batch
    on import_rollback_requests (workspace_id, import_batch_id)
    where status in ('queued', 'running');

create unique index job_queue_import_rollback_identity
    on job_queue (workspace_id, job_type, aggregate_id)
    where job_type = 'import_rollback';

create table import_rollback_conflicts (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    rollback_request_id uuid not null,
    import_batch_id uuid not null,
    conflict_seq bigint not null,
    conflict_type text not null,
    target_kind text,
    target_id uuid,
    expected_row_version bigint,
    current_row_version bigint,
    dependency_kind text,
    detail_code text not null,
    created_at timestamptz not null default now(),
    constraint import_rollback_conflicts_workspace_identity unique (workspace_id, id),
    constraint import_rollback_conflicts_request_sequence
        unique (workspace_id, rollback_request_id, conflict_seq),
    constraint import_rollback_conflicts_request_batch_fk
        foreign key (workspace_id, rollback_request_id, import_batch_id)
        references import_rollback_requests(workspace_id, id, import_batch_id)
        on delete restrict,
    constraint import_rollback_conflicts_sequence_positive check (conflict_seq > 0),
    constraint import_rollback_conflicts_type_allowed
        check (
            conflict_type in (
                'rollback_not_available',
                'target_missing',
                'target_version_changed',
                'target_data_changed',
                'later_import',
                'later_modification',
                'downstream_dependency',
                'change_log_incomplete',
                'source_chain_broken',
                'illegal_change'
            )
        ),
    constraint import_rollback_conflicts_target_kind_allowed
        check (target_kind is null or target_kind = 'imported_record'),
    constraint import_rollback_conflicts_versions_positive
        check (
            (expected_row_version is null or expected_row_version > 0)
            and (current_row_version is null or current_row_version > 0)
        ),
    constraint import_rollback_conflicts_detail_not_blank
        check (length(trim(detail_code)) > 0)
);

create index import_rollback_conflicts_workspace_page_idx
    on import_rollback_conflicts (
        workspace_id,
        rollback_request_id,
        conflict_seq,
        id
    );

create index import_rollback_conflicts_workspace_target_idx
    on import_rollback_conflicts (
        workspace_id,
        target_kind,
        target_id,
        rollback_request_id
    )
    where target_id is not null;

create table import_data_invalidations (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    import_batch_id uuid not null,
    rollback_request_id uuid not null,
    target_kind text not null,
    target_id uuid not null,
    invalidation_kind text not null,
    created_at timestamptz not null default now(),
    constraint import_data_invalidations_workspace_identity unique (workspace_id, id),
    constraint import_data_invalidations_request_target_identity
        unique (
            workspace_id,
            rollback_request_id,
            target_kind,
            target_id,
            invalidation_kind
        ),
    constraint import_data_invalidations_request_batch_fk
        foreign key (workspace_id, rollback_request_id, import_batch_id)
        references import_rollback_requests(workspace_id, id, import_batch_id)
        on delete restrict,
    constraint import_data_invalidations_target_kind_allowed
        check (target_kind = 'imported_record'),
    constraint import_data_invalidations_kind_allowed
        check (invalidation_kind = 'import_rollback')
);

create index import_data_invalidations_workspace_batch_idx
    on import_data_invalidations (workspace_id, import_batch_id, created_at, id);

create table object_consistency_runs (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    status text not null default 'running',
    requested_by uuid not null references users(id) on delete restrict,
    root_fingerprint char(64) not null,
    scanned_object_count bigint not null default 0,
    finding_count bigint not null default 0,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    constraint object_consistency_runs_workspace_identity unique (workspace_id, id),
    constraint object_consistency_runs_status_allowed
        check (status in ('running', 'completed', 'failed')),
    constraint object_consistency_runs_root_fingerprint_hex
        check (root_fingerprint ~ '^[0-9a-f]{64}$'),
    constraint object_consistency_runs_counts_nonnegative
        check (scanned_object_count >= 0 and finding_count >= 0),
    constraint object_consistency_runs_finished_binding
        check (
            (status = 'running' and finished_at is null)
            or (status in ('completed', 'failed') and finished_at is not null)
        )
);

create index object_consistency_runs_workspace_started_idx
    on object_consistency_runs (workspace_id, started_at desc, id);

create table object_consistency_findings (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    run_id uuid not null,
    stored_object_id uuid,
    finding_type text not null,
    observed_object_key text,
    observed_sha256 char(64),
    observed_size_bytes bigint,
    disposition_status text not null default 'detected',
    detected_at timestamptz not null default now(),
    constraint object_consistency_findings_workspace_identity unique (workspace_id, id),
    constraint object_consistency_findings_run_identity
        unique (workspace_id, run_id, id),
    constraint object_consistency_findings_run_fk
        foreign key (workspace_id, run_id)
        references object_consistency_runs(workspace_id, id) on delete restrict,
    constraint object_consistency_findings_stored_object_fk
        foreign key (workspace_id, stored_object_id)
        references stored_objects(workspace_id, id) on delete restrict,
    constraint object_consistency_findings_type_allowed
        check (
            finding_type in (
                'database_object_missing',
                'orphan_object',
                'size_mismatch',
                'sha256_mismatch',
                'backend_mismatch',
                'state_mismatch',
                'workspace_path_mismatch',
                'stale_temporary_object',
                'stale_pending_object',
                'commit_outcome_unknown'
            )
        ),
    constraint object_consistency_findings_sha256_hex
        check (observed_sha256 is null or observed_sha256 ~ '^[0-9a-f]{64}$'),
    constraint object_consistency_findings_size_nonnegative
        check (observed_size_bytes is null or observed_size_bytes >= 0),
    constraint object_consistency_findings_disposition_allowed
        check (disposition_status in ('detected', 'quarantined', 'acknowledged')),
    constraint object_consistency_findings_source_present
        check (stored_object_id is not null or observed_object_key is not null),
    constraint object_consistency_findings_key_not_blank
        check (
            observed_object_key is null
            or length(trim(observed_object_key)) > 0
        )
);

create index object_consistency_findings_workspace_run_idx
    on object_consistency_findings (
        workspace_id,
        run_id,
        disposition_status,
        finding_type,
        detected_at,
        id
    );

create index object_consistency_findings_workspace_object_idx
    on object_consistency_findings (workspace_id, stored_object_id, detected_at desc)
    where stored_object_id is not null;

create table object_quarantines (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    finding_id uuid not null,
    stored_object_id uuid,
    source_object_key text not null,
    quarantine_object_key text not null,
    sha256 char(64) not null,
    size_bytes bigint not null,
    quarantined_by uuid not null references users(id) on delete restrict,
    quarantined_at timestamptz not null default now(),
    disposition_status text not null default 'quarantined',
    constraint object_quarantines_workspace_identity unique (workspace_id, id),
    constraint object_quarantines_finding_identity unique (workspace_id, finding_id),
    constraint object_quarantines_finding_fk
        foreign key (workspace_id, finding_id)
        references object_consistency_findings(workspace_id, id) on delete restrict,
    constraint object_quarantines_stored_object_fk
        foreign key (workspace_id, stored_object_id)
        references stored_objects(workspace_id, id) on delete restrict,
    constraint object_quarantines_source_key_not_blank
        check (length(trim(source_object_key)) > 0),
    constraint object_quarantines_target_key_not_blank
        check (length(trim(quarantine_object_key)) > 0),
    constraint object_quarantines_distinct_keys
        check (source_object_key <> quarantine_object_key),
    constraint object_quarantines_sha256_hex check (sha256 ~ '^[0-9a-f]{64}$'),
    constraint object_quarantines_size_nonnegative check (size_bytes >= 0),
    constraint object_quarantines_disposition_allowed
        check (disposition_status = 'quarantined')
);

create index object_quarantines_workspace_created_idx
    on object_quarantines (workspace_id, quarantined_at desc, id);

alter table stored_objects
    drop constraint stored_objects_state,
    add constraint stored_objects_state
        check (state in ('pending', 'available', 'quarantined', 'deleting', 'deleted'));

create or replace function app.prevent_phase_3d_immutable_row_change()
returns trigger
language plpgsql
as $$
begin
    raise exception '% rows are immutable', tg_table_name
        using errcode = '23514';
end;
$$;

create or replace function app.enforce_import_row_change_target()
returns trigger
language plpgsql
as $$
begin
    if new.target_kind = 'imported_record'
       and not exists (
            select 1
              from imported_records target
             where target.workspace_id = new.workspace_id
               and target.id = new.target_id
       ) then
        raise exception 'change target is not visible in the batch workspace'
            using errcode = '23514';
    end if;
    return new;
end;
$$;

create or replace function app.enforce_import_change_sequence()
returns trigger
language plpgsql
as $$
declare
    change_count bigint;
    maximum_sequence bigint;
begin
    select count(*), coalesce(max(sequence_no), 0)
      into change_count, maximum_sequence
      from import_row_changes
     where workspace_id = new.workspace_id
       and import_batch_id = new.import_batch_id;
    if change_count <> maximum_sequence then
        raise exception 'import change sequence must be contiguous from one'
            using errcode = '23514';
    end if;
    return null;
end;
$$;

create or replace function app.enforce_import_batch_phase_3d_invariants()
returns trigger
language plpgsql
as $$
begin
    if tg_op = 'UPDATE'
       and old.committed_at is not null
       and (
            new.rollback_capability is distinct from old.rollback_capability
            or new.change_log_version is distinct from old.change_log_version
       ) then
        raise exception 'rollback capability is immutable after import success'
            using errcode = '23514';
    end if;

    if tg_op = 'UPDATE'
       and old.status not in ('uploaded', 'inspected', 'mapped', 'preview_ready')
       and new.compensates_batch_id is distinct from old.compensates_batch_id then
        raise exception 'compensation lineage is immutable after confirmation'
            using errcode = '23514';
    end if;

    if new.compensates_batch_id is not null
       and not exists (
            select 1
              from import_batches parent
             where parent.workspace_id = new.workspace_id
               and parent.id = new.compensates_batch_id
               and parent.status in (
                    'succeeded',
                    'rollback_conflict',
                    'rolled_back',
                    'rollback_failed'
               )
       ) then
        raise exception 'compensation must reference an ended batch in the same workspace'
            using errcode = '23514';
    end if;

    if new.compensates_batch_id is not null
       and exists (
            with recursive ancestors(id, compensates_batch_id) as (
                select parent.id, parent.compensates_batch_id
                  from import_batches parent
                 where parent.workspace_id = new.workspace_id
                   and parent.id = new.compensates_batch_id
                union
                select parent.id, parent.compensates_batch_id
                  from import_batches parent
                  join ancestors child
                    on child.compensates_batch_id = parent.id
                 where parent.workspace_id = new.workspace_id
            )
            select 1 from ancestors where id = new.id
       ) then
        raise exception 'compensation lineage cannot contain a cycle'
            using errcode = '23514';
    end if;
    return new;
end;
$$;

create or replace function app.enforce_rollback_request_job()
returns trigger
language plpgsql
as $$
begin
    if new.job_id is null then
        return new;
    end if;
    if not exists (
        select 1
          from job_queue job
         where job.workspace_id = new.workspace_id
           and job.id = new.job_id
           and job.aggregate_id = new.import_batch_id
           and job.job_type = 'import_rollback'
    ) then
        raise exception 'rollback request must reference its controlled rollback job'
            using errcode = '23514';
    end if;
    return new;
end;
$$;

create or replace function app.enforce_rollback_request_transition()
returns trigger
language plpgsql
as $$
begin
    if tg_op = 'INSERT'
       and new.status not in ('prechecked', 'precheck_conflict') then
        raise exception 'rollback request must persist a synchronous precheck before queueing'
            using errcode = '23514';
    end if;

    if tg_op = 'UPDATE'
       and new.status is distinct from old.status
       and (old.status, new.status) not in (
            ('prechecked', 'queued'),
            ('queued', 'running'),
            ('running', 'succeeded'),
            ('running', 'worker_conflict'),
            ('running', 'failed')
       ) then
        raise exception 'invalid rollback request status transition'
            using errcode = '23514';
    end if;
    return new;
end;
$$;

create or replace function app.enforce_rollback_conflict_count()
returns trigger
language plpgsql
as $$
declare
    persisted_conflict_count bigint;
begin
    select count(*)
      into persisted_conflict_count
      from import_rollback_conflicts conflict_row
     where conflict_row.workspace_id = new.workspace_id
       and conflict_row.rollback_request_id = new.id;
    if persisted_conflict_count <> new.conflict_count then
        raise exception 'rollback conflict count must match its immutable conflict snapshot'
            using errcode = '23514';
    end if;
    return null;
end;
$$;

create or replace function app.enforce_rollback_conflict_parent_count()
returns trigger
language plpgsql
as $$
declare
    expected_conflict_count bigint;
    persisted_conflict_count bigint;
begin
    select request.conflict_count
      into expected_conflict_count
      from import_rollback_requests request
     where request.workspace_id = new.workspace_id
       and request.id = new.rollback_request_id;
    select count(*)
      into persisted_conflict_count
      from import_rollback_conflicts conflict_row
     where conflict_row.workspace_id = new.workspace_id
       and conflict_row.rollback_request_id = new.rollback_request_id;
    if expected_conflict_count is null
       or expected_conflict_count <> persisted_conflict_count then
        raise exception 'rollback conflict snapshot cannot diverge from its request'
            using errcode = '23514';
    end if;
    return null;
end;
$$;

create or replace function app.enforce_direct_rollback_change_log()
returns trigger
language plpgsql
as $$
declare
    change_count bigint;
begin
    if new.rollback_capability <> 'direct' then
        return null;
    end if;
    select count(*)
      into change_count
      from import_row_changes change
     where change.workspace_id = new.workspace_id
       and change.import_batch_id = new.id;
    if change_count <> (new.imported_count::bigint + new.overwritten_count::bigint) then
        raise exception 'direct rollback requires a complete change log'
            using errcode = '23514';
    end if;
    return null;
end;
$$;

create or replace function app.prevent_stored_object_delete_state()
returns trigger
language plpgsql
as $$
begin
    if new.state is distinct from old.state
       and new.state in ('deleting', 'deleted') then
        raise exception 'Phase 3D does not permit physical object deletion'
            using errcode = '23514';
    end if;
    return new;
end;
$$;

create trigger import_row_changes_validate_target
before insert on import_row_changes
for each row
execute function app.enforce_import_row_change_target();

create trigger import_row_changes_immutable
before update or delete on import_row_changes
for each row
execute function app.prevent_phase_3d_immutable_row_change();

create constraint trigger import_row_changes_contiguous_sequence
after insert on import_row_changes
deferrable initially deferred
for each row
execute function app.enforce_import_change_sequence();

create trigger import_rollback_conflicts_immutable
before update or delete on import_rollback_conflicts
for each row
execute function app.prevent_phase_3d_immutable_row_change();

create trigger import_data_invalidations_immutable
before update or delete on import_data_invalidations
for each row
execute function app.prevent_phase_3d_immutable_row_change();

create trigger object_quarantines_immutable
before update or delete on object_quarantines
for each row
execute function app.prevent_phase_3d_immutable_row_change();

create trigger import_batches_enforce_phase_3d_invariants
before insert or update on import_batches
for each row
execute function app.enforce_import_batch_phase_3d_invariants();

create trigger import_rollback_requests_validate_job
before insert or update of workspace_id, import_batch_id, job_id on import_rollback_requests
for each row
execute function app.enforce_rollback_request_job();

create trigger import_rollback_requests_enforce_transition
before insert or update of status on import_rollback_requests
for each row
execute function app.enforce_rollback_request_transition();

create constraint trigger import_rollback_requests_validate_conflict_count
after insert or update of status, conflict_count on import_rollback_requests
deferrable initially deferred
for each row
execute function app.enforce_rollback_conflict_count();

create constraint trigger import_rollback_conflicts_validate_parent_count
after insert on import_rollback_conflicts
deferrable initially deferred
for each row
execute function app.enforce_rollback_conflict_parent_count();

create constraint trigger import_batches_validate_direct_change_log
after insert or update of rollback_capability, change_log_version,
    imported_count, overwritten_count on import_batches
deferrable initially deferred
for each row
execute function app.enforce_direct_rollback_change_log();

create trigger stored_objects_prevent_delete_state
before update of state on stored_objects
for each row
execute function app.prevent_stored_object_delete_state();

alter table import_job_events
    drop constraint import_job_events_type_allowed,
    add constraint import_job_events_type_allowed
        check (
            event_type in (
                'queued',
                'running',
                'progress',
                'succeeded',
                'failed',
                'dead_letter',
                'rollback_queued',
                'rollback_running',
                'rollback_conflict',
                'rolled_back',
                'rollback_failed'
            )
        );

alter table import_row_changes enable row level security;
alter table import_row_changes force row level security;
alter table import_rollback_requests enable row level security;
alter table import_rollback_requests force row level security;
alter table import_rollback_conflicts enable row level security;
alter table import_rollback_conflicts force row level security;
alter table import_data_invalidations enable row level security;
alter table import_data_invalidations force row level security;
alter table object_consistency_runs enable row level security;
alter table object_consistency_runs force row level security;
alter table object_consistency_findings enable row level security;
alter table object_consistency_findings force row level security;
alter table object_quarantines enable row level security;
alter table object_quarantines force row level security;

create policy import_row_changes_workspace_isolation on import_row_changes
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy import_rollback_requests_workspace_isolation on import_rollback_requests
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy import_rollback_conflicts_workspace_isolation on import_rollback_conflicts
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy import_data_invalidations_workspace_isolation on import_data_invalidations
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy object_consistency_runs_workspace_isolation on object_consistency_runs
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy object_consistency_findings_workspace_isolation on object_consistency_findings
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy object_quarantines_workspace_isolation on object_quarantines
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

grant select, insert on import_row_changes to futures_runtime;
grant select, insert, update on import_rollback_requests to futures_runtime;
grant select, insert on import_rollback_conflicts to futures_runtime;
grant select, insert on import_data_invalidations to futures_runtime;
grant select, insert, update on object_consistency_runs to futures_runtime;
grant select, insert, update on object_consistency_findings to futures_runtime;
grant select, insert on object_quarantines to futures_runtime;
grant update (state, updated_at) on stored_objects to futures_runtime;
revoke delete on stored_objects from futures_runtime;

insert into schema_versions (version, description)
values ('202607260001', 'phase 3d rollback and object governance foundation')
on conflict (version) do nothing;

commit;

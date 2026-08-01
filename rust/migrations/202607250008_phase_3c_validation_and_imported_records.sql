begin;

alter table import_batches
    add column staging_version bigint not null default 0,
    add column validated_staging_version bigint,
    add column validation_version integer,
    add column validated_mapping_id uuid,
    add column validated_mapping_hash char(64),
    add column validated_at timestamptz,
    add column blocking_error_count integer not null default 0,
    add column warning_count integer not null default 0,
    add column duplicate_count integer not null default 0,
    add column conflict_count integer not null default 0,
    add column conflict_policy text,
    add column confirmation_fingerprint char(64),
    add column confirmed_by uuid references users(id) on delete restrict,
    add column confirmed_at timestamptz,
    add column committed_at timestamptz,
    add column processed_count integer not null default 0,
    add column imported_count integer not null default 0,
    add column skipped_count integer not null default 0,
    add column overwritten_count integer not null default 0,
    add column conflict_result_count integer not null default 0,
    add constraint import_batches_workspace_staging_identity
        unique (workspace_id, id, staging_version),
    add constraint import_batches_validated_mapping_fk
        foreign key (workspace_id, validated_mapping_id)
        references import_mappings(workspace_id, id) on delete restrict,
    add constraint import_batches_staging_version_nonnegative
        check (staging_version >= 0),
    add constraint import_batches_validation_version_positive
        check (validation_version is null or validation_version > 0),
    add constraint import_batches_validated_staging_version_valid
        check (
            validated_staging_version is null
            or (
                validated_staging_version > 0
                and validated_staging_version <= staging_version
            )
        ),
    add constraint import_batches_validated_mapping_hash_hex
        check (
            validated_mapping_hash is null
            or validated_mapping_hash ~ '^[0-9a-f]{64}$'
        ),
    add constraint import_batches_confirmation_fingerprint_hex
        check (
            confirmation_fingerprint is null
            or confirmation_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    add constraint import_batches_conflict_policy_allowed
        check (
            conflict_policy is null
            or conflict_policy in ('skip', 'overwrite', 'keep_conflict', 'abort')
        ),
    add constraint import_batches_validation_binding_complete
        check (
            (
                validated_at is null
                and validated_staging_version is null
                and validation_version is null
                and validated_mapping_id is null
                and validated_mapping_hash is null
            )
            or (
                validated_at is not null
                and validated_staging_version is not null
                and validation_version is not null
                and validated_mapping_id is not null
                and validated_mapping_hash is not null
            )
        ),
    add constraint import_batches_confirmation_binding_complete
        check (
            (
                confirmed_at is null
                and confirmed_by is null
                and conflict_policy is null
                and confirmation_fingerprint is null
            )
            or (
                confirmed_at is not null
                and confirmed_by is not null
                and conflict_policy is not null
                and confirmation_fingerprint is not null
            )
        ),
    add constraint import_batches_counts_nonnegative
        check (
            blocking_error_count >= 0
            and warning_count >= 0
            and duplicate_count >= 0
            and conflict_count >= 0
            and processed_count >= 0
            and imported_count >= 0
            and skipped_count >= 0
            and overwritten_count >= 0
            and conflict_result_count >= 0
        );

update import_batches batch
   set staging_version = 1
 where exists (
    select 1
      from import_staging_rows staging
     where staging.workspace_id = batch.workspace_id
       and staging.import_batch_id = batch.id
 );

alter table import_staging_rows
    add column staging_version bigint not null default 1,
    add column validation_version integer,
    add column business_key text,
    add column record_data jsonb,
    add column is_file_duplicate boolean not null default false,
    add column has_database_conflict boolean not null default false,
    add column validated_at timestamptz,
    add constraint import_staging_rows_batch_version_fk
        foreign key (workspace_id, import_batch_id, staging_version)
        references import_batches(workspace_id, id, staging_version) on delete restrict,
    add constraint import_staging_rows_staging_version_positive
        check (staging_version > 0),
    add constraint import_staging_rows_validation_version_positive
        check (validation_version is null or validation_version > 0),
    add constraint import_staging_rows_business_key_not_blank
        check (business_key is null or length(trim(business_key)) > 0),
    add constraint import_staging_rows_record_data_object
        check (record_data is null or jsonb_typeof(record_data) = 'object'),
    add constraint import_staging_rows_validation_binding_complete
        check (
            (
                validated_at is null
                and validation_version is null
                and business_key is null
                and record_data is null
                and not is_file_duplicate
                and not has_database_conflict
            )
            or (
                validated_at is not null
                and validation_version is not null
                and record_data is not null
            )
        );

alter table import_errors
    add column staging_row_id uuid,
    add column staging_version bigint,
    add column validation_version integer,
    add column error_kind text not null default 'validation',
    add constraint import_errors_staging_row_fk
        foreign key (workspace_id, staging_row_id)
        references import_staging_rows(workspace_id, id) on delete restrict,
    add constraint import_errors_batch_version_fk
        foreign key (workspace_id, import_batch_id, staging_version)
        references import_batches(workspace_id, id, staging_version) on delete restrict,
    add constraint import_errors_staging_version_positive
        check (staging_version is null or staging_version > 0),
    add constraint import_errors_validation_version_positive
        check (validation_version is null or validation_version > 0),
    add constraint import_errors_kind_allowed
        check (error_kind in ('validation', 'duplicate', 'conflict'));

create table imported_records (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    dataset_type text not null,
    business_key text not null,
    record_data jsonb not null,
    source_import_batch_id uuid not null,
    source_row_number integer not null,
    row_version bigint not null default 1,
    created_by uuid not null references users(id) on delete restrict,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint imported_records_workspace_identity unique (workspace_id, id),
    constraint imported_records_business_identity
        unique (workspace_id, dataset_type, business_key),
    constraint imported_records_source_batch_fk
        foreign key (workspace_id, source_import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint imported_records_dataset_not_blank
        check (length(trim(dataset_type)) > 0),
    constraint imported_records_business_key_not_blank
        check (length(trim(business_key)) > 0),
    constraint imported_records_data_object
        check (jsonb_typeof(record_data) = 'object'),
    constraint imported_records_source_row_positive
        check (source_row_number > 0),
    constraint imported_records_row_version_positive
        check (row_version > 0)
);

create table import_conflict_candidates (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    import_batch_id uuid not null,
    staging_row_id uuid not null,
    dataset_type text not null,
    business_key text not null,
    candidate_data jsonb not null,
    existing_record_id uuid,
    conflict_kind text not null,
    created_at timestamptz not null default now(),
    constraint import_conflict_candidates_workspace_identity unique (workspace_id, id),
    constraint import_conflict_candidates_retry_identity
        unique (workspace_id, import_batch_id, staging_row_id, conflict_kind),
    constraint import_conflict_candidates_batch_fk
        foreign key (workspace_id, import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint import_conflict_candidates_staging_fk
        foreign key (workspace_id, staging_row_id)
        references import_staging_rows(workspace_id, id) on delete restrict,
    constraint import_conflict_candidates_existing_record_fk
        foreign key (workspace_id, existing_record_id)
        references imported_records(workspace_id, id) on delete restrict,
    constraint import_conflict_candidates_dataset_not_blank
        check (length(trim(dataset_type)) > 0),
    constraint import_conflict_candidates_business_key_not_blank
        check (length(trim(business_key)) > 0),
    constraint import_conflict_candidates_data_object
        check (jsonb_typeof(candidate_data) = 'object'),
    constraint import_conflict_candidates_kind
        check (conflict_kind in ('file_duplicate', 'database_conflict')),
    constraint import_conflict_candidates_existing_record_semantics
        check (
            (conflict_kind = 'file_duplicate' and existing_record_id is null)
            or (
                conflict_kind = 'database_conflict'
                and existing_record_id is not null
            )
        )
);

create index import_batches_workspace_validation_idx
    on import_batches (workspace_id, status, validated_at desc);

create index import_staging_rows_workspace_validation_idx
    on import_staging_rows (
        workspace_id,
        import_batch_id,
        staging_version,
        validation_version,
        row_number
    );

create index import_staging_rows_workspace_business_key_idx
    on import_staging_rows (workspace_id, import_batch_id, business_key, row_number)
    where business_key is not null;

create index import_errors_workspace_cursor_idx
    on import_errors (
        workspace_id,
        import_batch_id,
        coalesce(row_number, 0),
        created_at,
        id
    );

create index imported_records_workspace_source_idx
    on imported_records (workspace_id, source_import_batch_id, source_row_number);

create index import_conflict_candidates_workspace_batch_idx
    on import_conflict_candidates (
        workspace_id,
        import_batch_id,
        business_key,
        staging_row_id
    );

create or replace function app.prevent_confirmed_import_input_changes()
returns trigger
language plpgsql
as $$
declare
    batch_status import_batch_status;
begin
    select status
      into batch_status
      from import_batches
     where workspace_id = old.workspace_id
       and id = old.import_batch_id;

    if batch_status not in ('uploaded', 'inspected', 'mapped', 'preview_ready') then
        raise exception 'confirmed import inputs are immutable'
            using errcode = '23514';
    end if;

    if tg_op = 'DELETE' then
        return old;
    end if;

    return new;
end;
$$;

create trigger import_mappings_prevent_confirmed_changes
before update or delete on import_mappings
for each row
execute function app.prevent_confirmed_import_input_changes();

create trigger import_staging_rows_prevent_confirmed_changes
before update or delete on import_staging_rows
for each row
execute function app.prevent_confirmed_import_input_changes();

create or replace function app.enforce_import_batch_frozen_confirmation()
returns trigger
language plpgsql
as $$
begin
    if old.status not in ('uploaded', 'inspected', 'mapped', 'preview_ready')
       and (
            new.workspace_id is distinct from old.workspace_id
            or new.id is distinct from old.id
            or new.dataset_type is distinct from old.dataset_type
            or new.staging_version is distinct from old.staging_version
            or new.validated_staging_version is distinct from old.validated_staging_version
            or new.validation_version is distinct from old.validation_version
            or new.validated_mapping_id is distinct from old.validated_mapping_id
            or new.validated_mapping_hash is distinct from old.validated_mapping_hash
            or new.validated_at is distinct from old.validated_at
            or new.conflict_policy is distinct from old.conflict_policy
            or new.confirmation_fingerprint is distinct from old.confirmation_fingerprint
            or new.confirmed_by is distinct from old.confirmed_by
            or new.confirmed_at is distinct from old.confirmed_at
       ) then
        raise exception 'confirmed import parameters are immutable'
            using errcode = '23514';
    end if;

    if new.staging_version is distinct from old.staging_version
       or (
            old.status = 'preview_ready'
            and new.status = 'mapped'
       ) then
        new.validated_staging_version := null;
        new.validation_version := null;
        new.validated_mapping_id := null;
        new.validated_mapping_hash := null;
        new.validated_at := null;
        new.blocking_error_count := 0;
        new.warning_count := 0;
        new.duplicate_count := 0;
        new.conflict_count := 0;
    end if;

    return new;
end;
$$;

create trigger import_batches_enforce_frozen_confirmation
before update on import_batches
for each row
execute function app.enforce_import_batch_frozen_confirmation();

alter table imported_records enable row level security;
alter table imported_records force row level security;
alter table import_conflict_candidates enable row level security;
alter table import_conflict_candidates force row level security;

create policy imported_records_workspace_isolation on imported_records
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy import_conflict_candidates_workspace_isolation on import_conflict_candidates
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

grant select, insert, update on imported_records to futures_runtime;
grant select, insert on import_conflict_candidates to futures_runtime;
grant select, insert, update, delete on import_staging_rows to futures_runtime;

insert into schema_versions (version, description)
values ('202607250008', 'phase 3c validation and imported records')
on conflict (version) do nothing;

commit;

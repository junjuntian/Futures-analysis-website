begin;

create type import_batch_status as enum (
    'uploaded',
    'inspected',
    'mapped',
    'preview_ready',
    'confirmed',
    'importing',
    'succeeded',
    'failed',
    'cancelled',
    'rollback_check',
    'rolling_back',
    'rollback_conflict',
    'rolled_back',
    'rollback_failed',
    'expired'
);

create table stored_objects (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    object_key text not null unique,
    sha256 char(64) not null,
    size_bytes bigint not null,
    content_type text not null,
    created_by uuid not null references users(id) on delete restrict,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint stored_objects_workspace_identity unique (workspace_id, id),
    constraint stored_objects_key_not_blank check (length(trim(object_key)) > 0),
    constraint stored_objects_sha256_hex check (sha256 ~ '^[0-9a-f]{64}$'),
    constraint stored_objects_size_positive check (size_bytes > 0)
);

create index stored_objects_workspace_created_idx
    on stored_objects (workspace_id, created_at desc);

create table import_batches (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    status import_batch_status not null default 'uploaded',
    created_by uuid not null references users(id) on delete restrict,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint import_batches_workspace_identity unique (workspace_id, id)
);

create index import_batches_workspace_created_idx
    on import_batches (workspace_id, created_at desc);

create table import_files (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    import_batch_id uuid not null,
    stored_object_id uuid not null,
    original_filename text not null,
    declared_mime_type text not null,
    detected_format text not null,
    sha256 char(64) not null,
    size_bytes bigint not null,
    created_by uuid not null references users(id) on delete restrict,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint import_files_workspace_identity unique (workspace_id, id),
    constraint import_files_one_file_per_batch unique (workspace_id, import_batch_id),
    constraint import_files_batch_workspace_fk
        foreign key (workspace_id, import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint import_files_object_workspace_fk
        foreign key (workspace_id, stored_object_id)
        references stored_objects(workspace_id, id) on delete restrict,
    constraint import_files_name_not_blank check (length(trim(original_filename)) > 0),
    constraint import_files_mime_not_blank check (length(trim(declared_mime_type)) > 0),
    constraint import_files_format check (detected_format in ('txt', 'csv', 'xls', 'xlsx')),
    constraint import_files_sha256_hex check (sha256 ~ '^[0-9a-f]{64}$'),
    constraint import_files_size_positive check (size_bytes > 0)
);

create index import_files_workspace_batch_idx
    on import_files (workspace_id, import_batch_id);

create index import_files_workspace_object_idx
    on import_files (workspace_id, stored_object_id);

alter table stored_objects enable row level security;
alter table stored_objects force row level security;
alter table import_batches enable row level security;
alter table import_batches force row level security;
alter table import_files enable row level security;
alter table import_files force row level security;

create policy stored_objects_workspace_isolation on stored_objects
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy import_batches_workspace_isolation on import_batches
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy import_files_workspace_isolation on import_files
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

grant select, insert, delete on stored_objects to futures_runtime;
grant select, insert, update on import_batches to futures_runtime;
grant select, insert on import_files to futures_runtime;

insert into schema_versions (version, description)
values ('202607250001', 'phase 3a import upload foundation')
on conflict (version) do nothing;

commit;

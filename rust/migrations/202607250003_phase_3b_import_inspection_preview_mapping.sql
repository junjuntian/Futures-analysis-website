begin;

alter table import_batches
    add column dataset_type text not null default 'generic',
    add constraint import_batches_dataset_type_not_blank
        check (length(trim(dataset_type)) > 0);

alter table import_files
    add column detected_encoding text,
    add column detected_delimiter text,
    add column selected_sheet text,
    add column header_row integer,
    add column inspected_at timestamptz,
    add constraint import_files_header_row_positive
        check (header_row is null or header_row > 0);

create table import_templates (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    dataset_type text not null,
    name text not null,
    description text,
    created_by uuid not null references users(id) on delete restrict,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint import_templates_workspace_identity unique (workspace_id, id),
    constraint import_templates_workspace_name unique (workspace_id, dataset_type, name),
    constraint import_templates_dataset_not_blank check (length(trim(dataset_type)) > 0),
    constraint import_templates_name_not_blank check (length(trim(name)) > 0)
);

create table import_template_versions (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    template_id uuid not null,
    version_number integer not null,
    configuration_json jsonb not null,
    created_by uuid not null references users(id) on delete restrict,
    created_at timestamptz not null default now(),
    constraint import_template_versions_workspace_identity unique (workspace_id, id),
    constraint import_template_versions_template_fk
        foreign key (workspace_id, template_id)
        references import_templates(workspace_id, id) on delete restrict,
    constraint import_template_versions_one_number unique (workspace_id, template_id, version_number),
    constraint import_template_versions_number_positive check (version_number > 0),
    constraint import_template_versions_configuration_object
        check (jsonb_typeof(configuration_json) = 'object')
);

create table import_mappings (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    import_batch_id uuid not null,
    template_version_id uuid,
    dataset_type text not null,
    mapping_json jsonb not null,
    created_by uuid not null references users(id) on delete restrict,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint import_mappings_workspace_identity unique (workspace_id, id),
    constraint import_mappings_one_per_batch unique (workspace_id, import_batch_id),
    constraint import_mappings_batch_fk
        foreign key (workspace_id, import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint import_mappings_template_version_fk
        foreign key (workspace_id, template_version_id)
        references import_template_versions(workspace_id, id) on delete restrict,
    constraint import_mappings_dataset_not_blank check (length(trim(dataset_type)) > 0),
    constraint import_mappings_json_object check (jsonb_typeof(mapping_json) = 'object')
);

create table import_staging_rows (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    import_batch_id uuid not null,
    row_number integer not null,
    raw_values jsonb not null,
    normalized_values jsonb not null,
    target_fields jsonb not null,
    warnings jsonb not null default '[]'::jsonb,
    created_by uuid not null references users(id) on delete restrict,
    created_at timestamptz not null default now(),
    constraint import_staging_rows_workspace_identity unique (workspace_id, id),
    constraint import_staging_rows_batch_row unique (workspace_id, import_batch_id, row_number),
    constraint import_staging_rows_batch_fk
        foreign key (workspace_id, import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint import_staging_rows_positive_row check (row_number > 0),
    constraint import_staging_rows_raw_object check (jsonb_typeof(raw_values) = 'object'),
    constraint import_staging_rows_normalized_object check (jsonb_typeof(normalized_values) = 'object'),
    constraint import_staging_rows_target_object check (jsonb_typeof(target_fields) = 'object'),
    constraint import_staging_rows_warnings_array check (jsonb_typeof(warnings) = 'array')
);

create table import_errors (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete restrict,
    import_batch_id uuid not null,
    row_number integer,
    field_name text,
    severity text not null,
    error_code text not null,
    raw_value text,
    message text not null,
    created_by uuid not null references users(id) on delete restrict,
    created_at timestamptz not null default now(),
    constraint import_errors_workspace_identity unique (workspace_id, id),
    constraint import_errors_batch_fk
        foreign key (workspace_id, import_batch_id)
        references import_batches(workspace_id, id) on delete restrict,
    constraint import_errors_severity check (severity in ('error', 'warning')),
    constraint import_errors_row_positive check (row_number is null or row_number > 0),
    constraint import_errors_code_not_blank check (length(trim(error_code)) > 0),
    constraint import_errors_message_not_blank check (length(trim(message)) > 0)
);

create index import_templates_workspace_dataset_idx
    on import_templates (workspace_id, dataset_type, created_at desc);

create index import_template_versions_template_idx
    on import_template_versions (workspace_id, template_id, version_number desc);

create index import_mappings_batch_idx
    on import_mappings (workspace_id, import_batch_id);

create index import_staging_rows_batch_row_idx
    on import_staging_rows (workspace_id, import_batch_id, row_number);

create index import_errors_batch_row_idx
    on import_errors (workspace_id, import_batch_id, row_number nulls first, created_at);

alter table import_templates enable row level security;
alter table import_templates force row level security;
alter table import_template_versions enable row level security;
alter table import_template_versions force row level security;
alter table import_mappings enable row level security;
alter table import_mappings force row level security;
alter table import_staging_rows enable row level security;
alter table import_staging_rows force row level security;
alter table import_errors enable row level security;
alter table import_errors force row level security;

create policy import_templates_workspace_isolation on import_templates
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy import_template_versions_workspace_isolation on import_template_versions
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy import_mappings_workspace_isolation on import_mappings
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy import_staging_rows_workspace_isolation on import_staging_rows
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

create policy import_errors_workspace_isolation on import_errors
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

grant select, insert, update on import_batches to futures_runtime;
grant select, insert, update on import_files to futures_runtime;
grant select, insert, update on import_templates to futures_runtime;
grant select, insert on import_template_versions to futures_runtime;
grant select, insert, update on import_mappings to futures_runtime;
grant select, insert, delete on import_staging_rows to futures_runtime;
grant select, insert, delete on import_errors to futures_runtime;

insert into schema_versions (version, description)
values ('202607250003', 'phase 3b import inspection preview and mapping')
on conflict (version) do nothing;

commit;

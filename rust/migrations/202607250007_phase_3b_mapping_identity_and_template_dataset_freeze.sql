begin;

alter table import_template_versions
    add column dataset_type text;

drop trigger if exists import_template_versions_prevent_update on import_template_versions;

update import_template_versions version
   set dataset_type = template.dataset_type
  from import_templates template
 where template.workspace_id = version.workspace_id
   and template.id = version.template_id;

alter table import_template_versions
    alter column dataset_type set not null;

alter table import_template_versions
    add constraint import_template_versions_dataset_not_blank
    check (length(trim(dataset_type)) > 0);

create or replace function app.enforce_import_mapping_invariants()
returns trigger
language plpgsql
as $$
declare
    expected_dataset_type text;
    expected_fields jsonb;
    batch_status import_batch_status;
begin
    if tg_op = 'UPDATE'
       and (
            new.workspace_id is distinct from old.workspace_id
            or new.import_batch_id is distinct from old.import_batch_id
       ) then
        raise exception 'mapping workspace and import batch are immutable'
            using errcode = '23514';
    end if;

    if tg_op = 'UPDATE'
       and old.template_version_id is not null
       and new.template_version_id is distinct from old.template_version_id then
        raise exception 'template_version_id cannot be rebound once set'
            using errcode = '23514';
    end if;

    select status
      into batch_status
      from import_batches
     where workspace_id = new.workspace_id
       and id = new.import_batch_id;

    if not found then
        raise exception 'import batch is not visible for mapping'
            using errcode = '23514';
    end if;

    if new.template_version_id is not null then
        select version.dataset_type, version.configuration_json -> 'fields'
          into expected_dataset_type, expected_fields
          from import_template_versions version
         where version.workspace_id = new.workspace_id
           and version.id = new.template_version_id;

        if not found then
            raise exception 'template version is not visible in this workspace'
                using errcode = '23514';
        end if;

        if new.dataset_type is distinct from expected_dataset_type
           or not coalesce(app.import_mapping_fields_are_well_formed(expected_fields), false)
           or not coalesce(app.import_mapping_fields_are_well_formed(new.mapping_json -> 'fields'), false)
           or (new.mapping_json - 'fields') <> '{}'::jsonb
           or (new.mapping_json -> 'fields') is distinct from expected_fields then
            raise exception 'mapping must exactly match its template version'
                using errcode = '23514';
        end if;
    end if;

    if tg_op = 'INSERT' and batch_status = 'preview_ready' then
        raise exception 'mapping cannot change while preview is ready; regenerate preview first'
            using errcode = '23514';
    elsif tg_op = 'UPDATE'
       and batch_status = 'preview_ready'
       and (
            new.dataset_type is distinct from old.dataset_type
            or new.mapping_json is distinct from old.mapping_json
            or new.template_version_id is distinct from old.template_version_id
       ) then
        raise exception 'mapping cannot change while preview is ready; regenerate preview first'
            using errcode = '23514';
    end if;

    return new;
end;
$$;

create or replace function app.enforce_import_template_version_insert()
returns trigger
language plpgsql
as $$
declare
    parent_dataset_type text;
begin
    select dataset_type
      into parent_dataset_type
      from import_templates
     where workspace_id = new.workspace_id
       and id = new.template_id
     for update;

    if not found then
        raise exception 'template is not visible for this version'
            using errcode = '23514';
    end if;

    if new.dataset_type is distinct from parent_dataset_type then
        raise exception 'template version dataset_type must match its parent template'
            using errcode = '23514';
    end if;

    if not coalesce(
        app.import_mapping_fields_are_well_formed(new.configuration_json -> 'fields'),
        false
    ) or (new.configuration_json - 'fields') <> '{}'::jsonb then
        raise exception 'template version configuration is invalid'
            using errcode = '23514';
    end if;

    return new;
end;
$$;

create or replace function app.prevent_import_template_identity_update()
returns trigger
language plpgsql
as $$
begin
    if new.id is distinct from old.id
       or new.workspace_id is distinct from old.workspace_id then
        raise exception 'template identity is immutable'
            using errcode = '23514';
    end if;

    if new.dataset_type is distinct from old.dataset_type
       and exists (
            select 1
              from import_template_versions version
             where version.workspace_id = old.workspace_id
               and version.template_id = old.id
       ) then
        raise exception 'template dataset_type is immutable after its first version'
            using errcode = '23514';
    end if;

    return new;
end;
$$;

drop trigger if exists import_mappings_enforce_invariants on import_mappings;
create trigger import_mappings_enforce_invariants
before insert or update on import_mappings
for each row
execute function app.enforce_import_mapping_invariants();

drop trigger if exists import_template_versions_enforce_insert on import_template_versions;
create trigger import_template_versions_enforce_insert
before insert on import_template_versions
for each row
execute function app.enforce_import_template_version_insert();

drop trigger if exists import_template_versions_prevent_update on import_template_versions;
create trigger import_template_versions_prevent_update
before update on import_template_versions
for each row
execute function app.prevent_import_template_version_update();

drop trigger if exists import_templates_prevent_identity_update on import_templates;
create trigger import_templates_prevent_identity_update
before update on import_templates
for each row
execute function app.prevent_import_template_identity_update();

insert into schema_versions (version, description)
values ('202607250007', 'phase 3b mapping identity and template dataset freeze')
on conflict (version) do nothing;

commit;

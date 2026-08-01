begin;

-- 202607250005 compared TG_OP with lowercase literals. PostgreSQL supplies
-- INSERT/UPDATE in uppercase, so replace the deployed function explicitly.
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
        select template.dataset_type, version.configuration_json -> 'fields'
          into expected_dataset_type, expected_fields
          from import_template_versions version
          join import_templates template
            on template.workspace_id = version.workspace_id
           and template.id = version.template_id
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

insert into schema_versions (version, description)
values ('202607250006', 'phase 3b mapping trigger operation name correction')
on conflict (version) do nothing;

commit;

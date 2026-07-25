-- Run with a privileged PostgreSQL role after migrations 202607250004 through 202607250006.
-- The outer transaction always rolls back; no test user, workspace, batch, mapping, staging row,
-- or error row remains after a successful run.
\set ON_ERROR_STOP on

begin;

do $$
declare
    user_one uuid := '30000000-0000-7000-8000-000000000001';
    user_two uuid := '30000000-0000-7000-8000-000000000002';
    workspace_one uuid := '30000000-0000-7000-8000-000000000011';
    workspace_two uuid := '30000000-0000-7000-8000-000000000012';
    template_one uuid := '30000000-0000-7000-8000-000000000021';
    template_two uuid := '30000000-0000-7000-8000-000000000022';
    template_cross_workspace uuid := '30000000-0000-7000-8000-000000000023';
    version_one uuid := '30000000-0000-7000-8000-000000000031';
    version_two uuid := '30000000-0000-7000-8000-000000000032';
    version_cross_workspace uuid := '30000000-0000-7000-8000-000000000033';
    version_invalid_dataset uuid := '30000000-0000-7000-8000-000000000034';
    bound_batch uuid := '30000000-0000-7000-8000-000000000041';
    preview_batch uuid := '30000000-0000-7000-8000-000000000042';
    cross_workspace_batch uuid := '30000000-0000-7000-8000-000000000043';
    bound_mapping uuid := '30000000-0000-7000-8000-000000000051';
    preview_mapping uuid := '30000000-0000-7000-8000-000000000052';
    fields jsonb := '[{"source_column":"date","target_field":"trade_date","transform":"date_ymd"}]'::jsonb;
    mismatched_fields jsonb := '[{"source_column":"price","target_field":"price","transform":"decimal"}]'::jsonb;
begin
    insert into users (id, username, username_normalized, password_hash, password_params_version)
    values
        (user_one, 'phase3b-invariant-one', 'phase3b-invariant-one', 'test', 1),
        (user_two, 'phase3b-invariant-two', 'phase3b-invariant-two', 'test', 1);
    insert into workspaces (id, name, owner_user_id)
    values
        (workspace_one, 'phase3b-invariant-one', user_one),
        (workspace_two, 'phase3b-invariant-two', user_two);

    perform set_config('app.current_workspace_id', workspace_one::text, true);
    insert into import_templates (id, workspace_id, dataset_type, name, created_by)
    values
        (template_one, workspace_one, 'generic', 'phase3b-invariant-one', user_one),
        (template_two, workspace_one, 'generic', 'phase3b-invariant-two', user_one);
    insert into import_template_versions
        (id, workspace_id, template_id, version_number, dataset_type, configuration_json, created_by)
    values
        (version_one, workspace_one, template_one, 1, 'generic',
         jsonb_build_object('fields', fields), user_one),
        (version_two, workspace_one, template_two, 1, 'generic',
         jsonb_build_object('fields', fields), user_one);
    begin
        insert into import_template_versions
            (id, workspace_id, template_id, version_number, dataset_type,
             configuration_json, created_by)
        values
            (version_invalid_dataset, workspace_one, template_one, 2, 'changed',
             jsonb_build_object('fields', fields), user_one);
        raise exception 'expected template version dataset mismatch to fail';
    exception when check_violation then null;
    end;
    insert into import_batches (id, workspace_id, status, dataset_type, created_by)
    values (bound_batch, workspace_one, 'inspected', 'generic', user_one);

    -- A first binding may not use fields that differ from the selected frozen version.
    begin
        insert into import_mappings
            (id, workspace_id, import_batch_id, template_version_id, dataset_type, mapping_json, created_by)
        values
            ('30000000-0000-7000-8000-000000000061', workspace_one, bound_batch,
             version_one, 'generic', jsonb_build_object('fields', mismatched_fields), user_one);
        raise exception 'expected mismatched first template binding to fail';
    exception when check_violation then null;
    end;

    insert into import_mappings
        (id, workspace_id, import_batch_id, template_version_id, dataset_type, mapping_json, created_by)
    values
        (bound_mapping, workspace_one, bound_batch, version_one, 'generic',
         jsonb_build_object('fields', fields), user_one);

    begin
        update import_mappings set template_version_id = version_two where id = bound_mapping;
        raise exception 'expected template rebind to fail';
    exception when check_violation then null;
    end;
    begin
        update import_mappings set template_version_id = null where id = bound_mapping;
        raise exception 'expected clearing template binding to fail';
    exception when check_violation then null;
    end;
    begin
        update import_mappings
           set mapping_json = jsonb_build_object('fields', mismatched_fields)
         where id = bound_mapping;
        raise exception 'expected bound mapping field modification to fail';
    exception when check_violation then null;
    end;
    begin
        update import_mappings
           set mapping_json = jsonb_build_object(
               'fields', '[{"source_column":"date","target_field":"trade_date","transform":null}]'::jsonb
           )
         where id = bound_mapping;
        raise exception 'expected bound mapping transform modification to fail';
    exception when check_violation then null;
    end;
    begin
        update import_template_versions
           set configuration_json = jsonb_build_object('fields', mismatched_fields)
         where id = version_one;
        raise exception 'expected template version configuration update to fail';
    exception when check_violation then null;
    end;
    begin
        update import_templates
           set dataset_type = 'changed'
         where id = template_one;
        raise exception 'expected parent template dataset update to fail after version creation';
    exception when check_violation then null;
    end;
    if (select dataset_type from import_templates where id = template_one) <> 'generic'
       or (select dataset_type from import_template_versions where id = version_one) <> 'generic' then
        raise exception 'template or frozen version dataset_type changed after rejected update';
    end if;

    perform set_config('app.current_workspace_id', workspace_two::text, true);
    insert into import_templates (id, workspace_id, dataset_type, name, created_by)
    values (template_cross_workspace, workspace_two, 'generic', 'phase3b-invariant-cross', user_two);
    insert into import_template_versions
        (id, workspace_id, template_id, version_number, dataset_type, configuration_json, created_by)
    values
        (version_cross_workspace, workspace_two, template_cross_workspace, 1, 'generic',
         jsonb_build_object('fields', fields), user_two);

    perform set_config('app.current_workspace_id', workspace_one::text, true);
    begin
        insert into import_batches (id, workspace_id, status, dataset_type, created_by)
        values (cross_workspace_batch, workspace_one, 'inspected', 'generic', user_one);
        insert into import_mappings
            (id, workspace_id, import_batch_id, template_version_id, dataset_type, mapping_json, created_by)
        values
            ('30000000-0000-7000-8000-000000000062', workspace_one, cross_workspace_batch,
             version_cross_workspace, 'generic', jsonb_build_object('fields', fields), user_one);
        raise exception 'expected cross-workspace template binding to fail';
    exception when check_violation then null;
    end;

    insert into import_batches (id, workspace_id, status, dataset_type, created_by)
    values (preview_batch, workspace_one, 'mapped', 'generic', user_one);
    insert into import_mappings
        (id, workspace_id, import_batch_id, template_version_id, dataset_type, mapping_json, created_by)
    values
        (preview_mapping, workspace_one, preview_batch, version_one, 'generic',
         jsonb_build_object('fields', fields), user_one);
    update import_batches set status = 'preview_ready' where id = preview_batch;
    insert into import_staging_rows
        (id, workspace_id, import_batch_id, row_number, raw_values, normalized_values, target_fields, created_by)
    values
        ('30000000-0000-7000-8000-000000000071', workspace_one, preview_batch, 1,
         '{"date":"2026-07-25"}'::jsonb, '{"date":"2026-07-25"}'::jsonb,
         '{"date":"trade_date"}'::jsonb, user_one);
    insert into import_errors
        (id, workspace_id, import_batch_id, severity, error_code, message, created_by)
    values
        ('30000000-0000-7000-8000-000000000072', workspace_one, preview_batch,
         'warning', 'phase3b_test_warning', 'phase3b test warning', user_one);

    -- Direct ordinary-mapping mutation is rejected while persisted preview data exists.
    begin
        update import_mappings
           set mapping_json = jsonb_build_object('fields', mismatched_fields)
         where id = preview_mapping;
        raise exception 'expected preview-ready mapping update to fail';
    exception when check_violation then null;
    end;
    begin
        update import_mappings
           set import_batch_id = bound_batch
         where id = preview_mapping;
        raise exception 'expected mapping move from preview-ready source batch to fail';
    exception when check_violation then null;
    end;
    begin
        update import_mappings
           set import_batch_id = preview_batch
         where id = bound_mapping;
        raise exception 'expected mapping move into preview-ready target batch to fail';
    exception when check_violation then null;
    end;
    begin
        update import_mappings
           set workspace_id = workspace_two
         where id = bound_mapping;
        raise exception 'expected mapping workspace move to fail';
    exception when check_violation then null;
    end;
    begin
        update import_templates
           set dataset_type = 'changed'
         where id = template_one;
        raise exception 'expected parent template dataset update with preview-ready binding to fail';
    exception when check_violation then null;
    end;
    if (select status from import_batches where id = preview_batch) <> 'preview_ready' then
        raise exception 'preview-ready batch status changed after rejected direct update';
    end if;
    if (select import_batch_id from import_mappings where id = preview_mapping) <> preview_batch
       or (select import_batch_id from import_mappings where id = bound_mapping) <> bound_batch then
        raise exception 'mapping batch identity changed after rejected move';
    end if;
    if (select count(*) from import_staging_rows where import_batch_id = preview_batch) <> 1
       or (select count(*) from import_errors where import_batch_id = preview_batch) <> 1 then
        raise exception 'rejected direct update changed persisted preview data';
    end if;
end;
$$;

rollback;

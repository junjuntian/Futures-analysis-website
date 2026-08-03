begin;

-- The Phase 4A tables FORCE RLS.  The preceding migration intentionally runs
-- as the non-bypass migrator role, so data repairs must establish each tenant
-- context before touching workspace-scoped rows.
do $$
declare
    target_workspace_id uuid;
begin
    for target_workspace_id in
        select id from workspaces order by id
    loop
        perform set_config(
            'app.current_workspace_id',
            target_workspace_id::text,
            true
        );

        update data_sources
           set priority = 50,
               updated_at = now()
         where workspace_id = target_workspace_id
           and code = 'akshare_sina_dce_fallback'
           and priority <> 50;

        update imported_records record
           set business_key = upper(source.code) || '|' || record.business_key,
               updated_at = now()
          from import_batches batch
          join data_sources source
            on source.workspace_id = batch.workspace_id
           and source.id = batch.data_source_id
         where record.workspace_id = target_workspace_id
           and batch.workspace_id = target_workspace_id
           and record.source_import_batch_id = batch.id
           and batch.ingestion_mode = 'automatic'
           and record.business_key not like upper(source.code) || '|%';

        update import_staging_rows staging
           set business_key = upper(source.code) || '|' || staging.business_key
          from import_batches batch
          join data_sources source
            on source.workspace_id = batch.workspace_id
           and source.id = batch.data_source_id
         where staging.workspace_id = target_workspace_id
           and batch.workspace_id = target_workspace_id
           and staging.import_batch_id = batch.id
           and batch.ingestion_mode = 'automatic'
           and staging.business_key is not null
           and staging.business_key not like upper(source.code) || '|%';

        -- Only legacy v1 automatic batches lack formal-projection change rows.
        -- Preserve v2 batches created by the repaired Worker.
        update import_batches
           set rollback_capability = 'compensation_only',
               change_log_version = null
         where workspace_id = target_workspace_id
           and ingestion_mode = 'automatic'
           and rollback_capability = 'direct'
           and change_log_version = 1;
    end loop;
end $$;

insert into schema_versions (version, description)
values ('202608030002', 'Phase 4A RLS-aware source identity backfill')
on conflict (version) do nothing;

commit;

\set ON_ERROR_STOP on

-- Run after migrations through 202607260001. This script is read-only and
-- verifies the Phase 3D schema contract without creating business fixtures.
do $$
declare
    table_name text;
    phase_3d_tables constant text[] := array[
        'import_row_changes',
        'import_rollback_requests',
        'import_rollback_conflicts',
        'import_data_invalidations',
        'object_consistency_runs',
        'object_consistency_findings',
        'object_quarantines'
    ];
begin
    foreach table_name in array phase_3d_tables loop
        if not exists (
            select 1
              from pg_attribute attribute
              join pg_class relation on relation.oid = attribute.attrelid
              join pg_namespace namespace on namespace.oid = relation.relnamespace
             where namespace.nspname = 'public'
               and relation.relname = table_name
               and attribute.attname = 'workspace_id'
               and attribute.attnotnull
               and not attribute.attisdropped
        ) then
            raise exception '% must contain workspace_id not null', table_name;
        end if;

        if not exists (
            select 1
              from pg_class relation
              join pg_namespace namespace on namespace.oid = relation.relnamespace
             where namespace.nspname = 'public'
               and relation.relname = table_name
               and relation.relrowsecurity
               and relation.relforcerowsecurity
        ) then
            raise exception '% must enable and force RLS', table_name;
        end if;

        if not exists (
            select 1
              from pg_policies policy
             where policy.schemaname = 'public'
               and policy.tablename = table_name
               and policy.qual like '%current_workspace_id%'
               and policy.with_check like '%current_workspace_id%'
        ) then
            raise exception '% must have a Workspace RLS policy', table_name;
        end if;

        if not exists (
            select 1
              from pg_indexes index_definition
             where index_definition.schemaname = 'public'
               and index_definition.tablename = table_name
               and index_definition.indexdef ~ '\(workspace_id[,)]'
        ) then
            raise exception '% must have a workspace-leading index', table_name;
        end if;
    end loop;
end;
$$;

do $$
begin
    if exists (
        select 1
          from import_batches batch
         where batch.rollback_capability = 'direct'
           and (
                batch.change_log_version is null
                or (
                    select count(*)
                      from import_row_changes change_row
                     where change_row.workspace_id = batch.workspace_id
                       and change_row.import_batch_id = batch.id
                ) <> batch.imported_count::bigint + batch.overwritten_count::bigint
           )
    ) then
        raise exception 'direct rollback batches must have a complete versioned change log';
    end if;

    if not exists (
        select 1
          from pg_trigger trigger
          join pg_class relation on relation.oid = trigger.tgrelid
         where relation.relname = 'import_row_changes'
           and trigger.tgname = 'import_row_changes_immutable'
           and not trigger.tgisinternal
    ) then
        raise exception 'import_row_changes must be immutable';
    end if;

    if exists (
        select 1
          from pg_attribute attribute
          join pg_class relation on relation.oid = attribute.attrelid
         where relation.relname = 'import_rollback_requests'
           and attribute.attname in ('idempotency_key_hash', 'request_hash', 'job_id')
           and attribute.attnotnull
           and not attribute.attisdropped
    ) then
        raise exception 'synchronous rollback prechecks must not require idempotency or job fields';
    end if;

    if not exists (
        select 1
          from pg_trigger trigger
          join pg_class relation on relation.oid = trigger.tgrelid
         where relation.relname = 'import_rollback_requests'
           and trigger.tgname = 'import_rollback_requests_enforce_transition'
           and not trigger.tgisinternal
    ) then
        raise exception 'rollback requests must prove precheck before entering queued';
    end if;

    if not exists (
        select 1
          from pg_trigger trigger
          join pg_class relation on relation.oid = trigger.tgrelid
         where relation.relname = 'import_rollback_conflicts'
           and trigger.tgname = 'import_rollback_conflicts_validate_parent_count'
           and not trigger.tgisinternal
    ) then
        raise exception 'persisted rollback conflict pages must match request counts';
    end if;

    if not exists (
        select 1
          from pg_constraint constraint_definition
         where constraint_definition.conname = 'import_batches_compensation_workspace_fk'
           and constraint_definition.contype = 'f'
    ) then
        raise exception 'compensation lineage must use a same-Workspace foreign key';
    end if;

    if not exists (
        select 1
          from pg_constraint constraint_definition
         where constraint_definition.conname = 'import_row_changes_source_file_batch_fk'
           and constraint_definition.contype = 'f'
    ) then
        raise exception 'change rows must bind source file and batch in one Workspace';
    end if;

    if has_table_privilege('futures_runtime', 'stored_objects', 'delete') then
        raise exception 'runtime role must not physically delete stored object metadata';
    end if;

    if exists (
        select 1
          from information_schema.columns
         where table_schema = 'public'
           and table_name in (
                'object_consistency_runs',
                'object_consistency_findings',
                'object_quarantines'
           )
           and column_name in ('deleted_at', 'delete_after', 'physically_deleted_at')
    ) then
        raise exception 'Phase 3D object governance must not model physical deletion';
    end if;
end;
$$;

select 'PHASE3D_SCHEMA_INVARIANTS_PASS' as result;
